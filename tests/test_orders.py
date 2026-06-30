"""Purchasable offers — order creation, pricing, idempotent fulfillment, the seam."""

import dataclasses
import io

from conftest import login_owner, onboard_studio

from hestia.campaigns import create_campaign
from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.db import connect
from hestia.fulfillment import LabFulfillment, build_fulfillment, list_fulfillments
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.invoices import get_invoice, mark_paid
from hestia.jobs import drain
from hestia.orders import create_order, fulfill_for_invoice_token, list_orders
from hestia.proofing import toggle_favorite
from hestia.sales import create_or_update_offer
from hestia.tenants import create_tenant, get_tenant, set_tax_rate, tenant_flags


def _setup(conn, settings, *, name="Order Studio", with_client=True):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    pid = None
    if with_client:
        c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
        p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
        pid = p["id"]
    g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
    if pid:
        assign_gallery_to_project(conn, t["id"], g["id"], pid)
    imgs = [add_image(conn, _storage(settings), tenant_id=t["id"], gallery_id=g["id"],
                      filename=f"{i}.jpg", fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")
            for i in range(3)]
    publish_gallery(conn, t["id"], g["id"])
    offer = create_or_update_offer(conn, tenant=dict(t), gallery=dict(g), run_id=None,
                                   vision_summary={"hero_image_ids": [imgs[0]["id"]], "keeper_count": 3},
                                   flags=tenant_flags(t))
    conn.commit()
    return t, g, offer, imgs


def _storage(settings):
    from hestia.storage import LocalStorage
    return LocalStorage(settings.media_dir)


def test_create_order_from_bundle(conn, settings):
    t, g, offer, _ = _setup(conn, settings)
    res = create_order(conn, settings, tenant=dict(t), offer=offer, sku="print_set")
    assert res and res["order"]["status"] == "pending"
    assert res["order"]["amount_cents"] == 12000  # Signature Print Set
    inv = get_invoice(conn, t["id"], res["invoice"]["id"])
    assert inv["amount_cents"] == 12000 and inv["client_name"] == "Sarah"


def test_create_order_rejects_offer_for_foreign_tenant(conn, settings):
    t1, _g, offer, _ = _setup(conn, settings, name="A")
    t2 = create_tenant(conn, name="B", shoot_type="wedding")
    assert create_order(conn, settings, tenant=dict(t2), offer=offer, sku="print_set") is None
    assert list_orders(conn, t1["id"]) == []
    assert list_orders(conn, t2["id"]) == []


def test_unknown_sku_returns_none(conn, settings):
    t, g, offer, _ = _setup(conn, settings)
    assert create_order(conn, settings, tenant=dict(t), offer=offer, sku="nope") is None


def test_order_applies_studio_sales_tax(conn, settings):
    """A print sale is taxable — the order's invoice carries the studio's tax on top,
    while amount_cents stays the pre-tax subtotal (revenue)."""
    t, g, offer, _ = _setup(conn, settings)
    set_tax_rate(conn, t["id"], 850)                      # 8.5%
    res = create_order(conn, settings, tenant=get_tenant(conn, t["id"]), offer=offer, sku="print_set")
    inv = res["invoice"]
    assert inv["tax_cents"] == round(inv["amount_cents"] * 850 / 10000) and inv["tax_cents"] > 0
    assert inv["total_cents"] == inv["amount_cents"] + inv["tax_cents"]


def test_order_applies_active_sale(conn, settings):
    t, g, offer, _ = _setup(conn, settings)
    create_campaign(conn, tenant_id=t["id"], gallery_id=g["id"], headline="Sale",
                    discount_pct=25, days=7)
    res = create_order(conn, settings, tenant=dict(t), offer=offer, sku="print_set")
    assert res["order"]["amount_cents"] == 9000  # 12000 - 25%


def test_favorites_order_prices_live(conn, settings):
    t, g, offer, imgs = _setup(conn, settings)
    # no favorites yet → favorites package unavailable
    assert create_order(conn, settings, tenant=dict(t), offer=offer, sku="favorites") is None
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=imgs[0]["id"])
    toggle_favorite(conn, tenant_id=t["id"], gallery_id=g["id"], image_id=imgs[1]["id"])
    res = create_order(conn, settings, tenant=dict(t), offer=offer, sku="favorites")
    assert res["order"]["amount_cents"] == 3000  # 2 × $15


def test_fulfillment_idempotent_on_payment(conn, settings):
    t, g, offer, _ = _setup(conn, settings)
    res = create_order(conn, settings, tenant=dict(t), offer=offer, sku="print_set")
    token = res["invoice"]["token"]
    mark_paid(conn, token=token, provider="mock", ref="r")
    assert fulfill_for_invoice_token(conn, token) is True
    # order is paid; a fulfillment job is queued
    assert list_orders(conn, t["id"])[0]["status"] == "paid"
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE kind='fulfillment.submit'").fetchone()["n"] == 1
    # a second settle callback does NOT re-enqueue (idempotent)
    assert fulfill_for_invoice_token(conn, token) is False
    assert conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE kind='fulfillment.submit'").fetchone()["n"] == 1


def test_fulfillment_job_records_mock(conn, settings):
    t, g, offer, _ = _setup(conn, settings)
    res = create_order(conn, settings, tenant=dict(t), offer=offer, sku="print_set")
    token = res["invoice"]["token"]
    mark_paid(conn, token=token, provider="mock", ref="r")
    fulfill_for_invoice_token(conn, token)
    conn.commit()
    drain(settings.db_path, settings)
    f = list_fulfillments(conn, t["id"])
    oid = res["order"]["id"]
    assert f[oid]["backend"] == "mock" and f[oid]["status"] == "submitted" and f[oid]["provider_ref"]


def test_fulfillment_job_idempotent_on_retry(conn, settings):
    t, g, offer, _ = _setup(conn, settings)
    res = create_order(conn, settings, tenant=dict(t), offer=offer, sku="print_set")
    oid = res["order"]["id"]
    mark_paid(conn, token=res["invoice"]["token"], provider="mock", ref="r")
    fulfill_for_invoice_token(conn, res["invoice"]["token"])
    conn.commit()
    drain(settings.db_path, settings)
    # simulate an at-least-once retry of the same job — must NOT duplicate the lab order
    from hestia.fulfillment import _submit_fulfillment
    _submit_fulfillment(settings, {"order_id": oid})
    n = conn.execute("SELECT COUNT(*) AS n FROM fulfillment_orders WHERE order_id = ?",
                     (oid,)).fetchone()["n"]
    assert n == 1


def test_lab_backend_without_config_fails_safe(settings):
    lab_settings = dataclasses.replace(settings, fulfillment_backend="lab", fulfillment_api_key="")
    provider = build_fulfillment(lab_settings)
    assert isinstance(provider, LabFulfillment)
    result = provider.submit({"id": 1, "sku": "x", "name": "X", "amount_cents": 100})
    assert result.status == "failed"  # recorded, never raised


def test_default_backend_is_mock(settings):
    assert build_fulfillment(settings).backend == "mock"


def test_tenant_isolation(conn, settings):
    t1, g1, offer1, _ = _setup(conn, settings, name="A")
    create_order(conn, settings, tenant=dict(t1), offer=offer1, sku="print_set")
    t2 = create_tenant(conn, name="B", shoot_type="wedding")
    conn.commit()
    assert list_orders(conn, t2["id"]) == []


def _setup_http(app):
    conn = connect(app.state.settings.db_path)
    try:
        t = create_tenant(conn, name="Live Studio", shoot_type="wedding")
        c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
        p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
        g = create_gallery(conn, tenant_id=t["id"], title="Wedding")
        assign_gallery_to_project(conn, t["id"], g["id"], p["id"])
        img = add_image(conn, app.state.storage, tenant_id=t["id"], gallery_id=g["id"],
                        filename="a.jpg", fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")
        publish_gallery(conn, t["id"], g["id"])
        offer = create_or_update_offer(conn, tenant=dict(t), gallery=dict(g), run_id=None,
                                       vision_summary={"hero_image_ids": [img["id"]], "keeper_count": 1},
                                       flags=tenant_flags(t))
        conn.commit()
        return t, g, offer
    finally:
        conn.close()


def test_http_offer_purchasable_and_fulfilled(client, app):
    t, g, offer = _setup_http(app)
    page = client.get(f"/s/{t['slug']}/{offer['token']}")
    assert page.status_code == 200
    assert "disabled" not in page.text and "/order" in page.text  # Reserve is live

    # reserve a bundle → redirected to the pay page
    r = client.post(f"/s/{t['slug']}/{offer['token']}/order", data={"sku": "print_set"})
    assert "/pay/" in r.url.path
    pay_token = r.url.path.split("/pay/")[1]

    # pay (mock settles immediately) → order paid + fulfillment submitted
    client.post(f"/pay/{pay_token}/checkout")
    drain(app.state.settings.db_path, app.state.settings)

    conn = connect(app.state.settings.db_path)
    try:
        orders = list_orders(conn, t["id"], gallery_id=g["id"])
        fulfillments = list_fulfillments(conn, t["id"])
    finally:
        conn.close()
    assert orders and orders[0]["status"] == "paid"
    assert fulfillments[orders[0]["id"]]["status"] == "submitted"


def test_http_offer_rejects_malformed_foreign_gallery_reference(client, app):
    t, _g, offer = _setup_http(app)
    conn = connect(app.state.settings.db_path)
    try:
        other = create_tenant(conn, name="Other Studio", shoot_type="wedding")
        foreign_gallery = create_gallery(conn, tenant_id=other["id"], title="Foreign")
        conn.execute(
            "UPDATE offers SET gallery_id = ? WHERE id = ?",
            (foreign_gallery["id"], offer["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    assert client.get(f"/s/{t['slug']}/{offer['token']}").status_code == 404
    assert client.post(
        f"/s/{t['slug']}/{offer['token']}/order",
        data={"sku": "print_set"},
    ).status_code == 404


def test_http_owner_sees_orders(client, app):
    creds = onboard_studio(client, email="owner@example.com")
    login_owner(client, creds)
    # set up a gallery+offer+order under the onboarded tenant
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id, slug FROM tenants LIMIT 1").fetchone()
        tenant_id, slug = tid["id"], tid["slug"]
        g = create_gallery(conn, tenant_id=tenant_id, title="Wedding")
        img = add_image(conn, app.state.storage, tenant_id=tenant_id, gallery_id=g["id"],
                        filename="a.jpg", fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")
        publish_gallery(conn, tenant_id, g["id"])
        t_row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        offer = create_or_update_offer(conn, tenant=dict(t_row), gallery=dict(g), run_id=None,
                                       vision_summary={"hero_image_ids": [img["id"]], "keeper_count": 1},
                                       flags=tenant_flags(dict(t_row)))
        conn.commit()
    finally:
        conn.close()
    client.post(f"/s/{slug}/{offer['token']}/order", data={"sku": "print_set"})
    detail = client.get(f"/galleries/{g['id']}")
    assert "Orders · 1" in detail.text and "Signature Print Set" in detail.text

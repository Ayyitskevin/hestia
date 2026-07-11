"""End-to-end commerce spine: catalog → pipeline → offer → order → pay → fulfillment."""

import io

from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.fulfillment import list_fulfillments
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.invoices import mark_paid
from hestia.jobs import drain
from hestia.orders import create_order, fulfill_for_invoice_token
from hestia.pipeline import execute_run, start_run
from hestia.sales import DEFAULT_CATALOG, get_offer_for_gallery, set_tenant_catalog
from hestia.tenants import create_tenant
from hestia.vision import MockVisionProvider


def _storage(settings):
    from hestia.storage import LocalStorage

    return LocalStorage(settings.media_dir)


def test_commerce_spine_catalog_pipeline_order_pay_fulfillment(conn, storage, settings, db_path):
    """Custom catalog prices flow through pipeline offers into paid fulfillment jobs."""
    tenant = create_tenant(conn, name="Spine Studio", shoot_type="wedding")
    client = create_client(conn, tenant_id=tenant["id"], name="Sarah", email="sarah@example.com")
    project = create_project(conn, tenant_id=tenant["id"], name="Wedding", client_id=client["id"])
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="Wedding Day")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    for i in range(5):
        add_image(
            conn,
            _storage(settings),
            tenant_id=tenant["id"],
            gallery_id=gallery["id"],
            filename=f"{i}.jpg",
            fileobj=io.BytesIO(bytes([i]) * 16),
            content_type="image/jpeg",
        )
    set_tenant_catalog(
        conn,
        tenant["id"],
        items={
            **{sku: DEFAULT_CATALOG[sku] for sku in DEFAULT_CATALOG},
            "print_set": {**DEFAULT_CATALOG["print_set"], "price_cents": 9900},
        },
        favorite_print_cents=1500,
    )
    publish_gallery(conn, tenant["id"], gallery["id"])
    conn.commit()

    run = start_run(conn, tenant=tenant, gallery_id=gallery["id"])
    result = execute_run(
        db_path,
        settings,
        run["id"],
        storage=storage,
        provider=MockVisionProvider(),
    )
    assert result["status"] == "done"
    assert result["offer_url"]

    offer = get_offer_for_gallery(conn, tenant["id"], gallery["id"])
    assert offer is not None
    print_bundle = next(b for b in offer["bundles"] if b["sku"] == "print_set")
    assert print_bundle["price_cents"] == 9900

    order_res = create_order(conn, settings, tenant=dict(tenant), offer=offer, sku="print_set")
    assert order_res["order"]["amount_cents"] == 9900
    assert order_res["order"]["status"] == "pending"

    token = order_res["invoice"]["token"]
    mark_paid(conn, token=token, provider="mock", ref="commerce-spine")
    assert fulfill_for_invoice_token(conn, token) is True
    assert fulfill_for_invoice_token(conn, token) is False
    assert (
        conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE kind='fulfillment.submit'").fetchone()["n"]
        == 1
    )

    conn.commit()
    drain(settings.db_path, settings)
    fulfillments = list_fulfillments(conn, tenant["id"])
    oid = order_res["order"]["id"]
    assert oid in fulfillments
    assert fulfillments[oid]["backend"] == "mock"
    assert fulfillments[oid]["status"] == "submitted"

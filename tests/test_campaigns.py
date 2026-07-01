"""Sales campaigns — clamping, expiry, single-active, discount math, the funnel."""

import io

from conftest import login_owner, onboard_studio

from hestia.campaigns import (
    apply_discount,
    create_campaign,
    discount_bundle,
    end_campaign,
    gallery_sales_opportunity,
    get_active_campaign,
    get_campaign,
    launch_gallery_sales_campaign,
    send_gallery_sales_campaigns,
)
from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.db import connect
from hestia.delivery import enable_delivery
from hestia.email import list_emails
from hestia.galleries import add_image, create_gallery, publish_gallery
from hestia.sales import create_or_update_offer
from hestia.tenants import create_tenant, tenant_flags


def _tenant(conn, name="Sale Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def test_create_clamps_and_activates(conn):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    c = create_campaign(conn, tenant_id=t["id"], gallery_id=g["id"], headline="Sale",
                        discount_pct=200, days=0)
    assert c["discount_pct"] == 90  # clamped to MAX
    active = get_active_campaign(conn, g["id"])
    assert active and active["id"] == c["id"]


def test_create_rejects_foreign_gallery(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    g = create_gallery(conn, tenant_id=t1["id"], title="G")
    assert create_campaign(conn, tenant_id=t2["id"], gallery_id=g["id"], headline="Bad",
                           discount_pct=10, days=7) is None
    assert get_active_campaign(conn, g["id"]) is None


def test_active_campaign_ignores_malformed_foreign_gallery_row(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    g = create_gallery(conn, tenant_id=t1["id"], title="G")
    good = create_campaign(conn, tenant_id=t1["id"], gallery_id=g["id"], headline="Good",
                           discount_pct=10, days=7)
    conn.execute(
        "INSERT INTO sales_campaigns (tenant_id, gallery_id, headline, discount_pct, ends_at) "
        "VALUES (?, ?, 'Bad', 90, datetime('now', '+7 days'))",
        (t2["id"], g["id"]),
    )
    assert get_active_campaign(conn, g["id"])["id"] == good["id"]
    assert get_active_campaign(conn, g["id"], tenant_id=t1["id"])["id"] == good["id"]
    assert get_active_campaign(conn, g["id"], tenant_id=t2["id"]) is None


def test_launching_new_ends_prior(conn):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    first = create_campaign(conn, tenant_id=t["id"], gallery_id=g["id"], headline="A",
                            discount_pct=10, days=7)
    second = create_campaign(conn, tenant_id=t["id"], gallery_id=g["id"], headline="B",
                             discount_pct=20, days=7)
    assert get_active_campaign(conn, g["id"])["id"] == second["id"]
    assert get_campaign(conn, t["id"], first["id"])["status"] == "ended"


def test_expired_campaign_inactive(conn):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    c = create_campaign(conn, tenant_id=t["id"], gallery_id=g["id"], headline="Past",
                        discount_pct=10, days=7)
    conn.execute("UPDATE sales_campaigns SET ends_at = datetime('now', '-1 day') WHERE id = ?",
                 (c["id"],))
    assert get_active_campaign(conn, g["id"]) is None


def test_end_campaign(conn):
    t = _tenant(conn)
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    create_campaign(conn, tenant_id=t["id"], gallery_id=g["id"], headline="S", discount_pct=10, days=7)
    end_campaign(conn, t["id"], g["id"])
    assert get_active_campaign(conn, g["id"]) is None


def test_discount_math():
    assert apply_discount(10000, 20) == 8000
    assert apply_discount(10000, 0) == 10000
    assert apply_discount(10000, 200) == 1000  # clamped to 90% off
    b = {"price_cents": 12000, "price": "$120"}
    d = discount_bundle(b, 25)
    assert d["price_cents"] == 9000 and d["price"] == "$90" and d["orig_price"] == "$120"
    assert discount_bundle(b, 0) is b  # no-op at 0%


def test_tenant_isolation(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    g = create_gallery(conn, tenant_id=t1["id"], title="G")
    c = create_campaign(conn, tenant_id=t1["id"], gallery_id=g["id"], headline="S",
                        discount_pct=10, days=7)
    assert get_campaign(conn, t2["id"], c["id"]) is None


def _ready_sales_gallery(conn, storage, settings, tenant, *, email="sarah@example.com", title="Wedding"):
    client = create_client(conn, tenant_id=tenant["id"], name="Sarah", email=email)
    project = create_project(conn, tenant_id=tenant["id"], name=title, client_id=client["id"])
    gallery = create_gallery(conn, tenant_id=tenant["id"], title=title)
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    img = add_image(conn, storage, tenant_id=tenant["id"], gallery_id=gallery["id"],
                    filename="a.jpg", fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")
    publish_gallery(conn, tenant["id"], gallery["id"])
    enable_delivery(conn, tenant["id"], gallery["id"])
    offer = create_or_update_offer(conn, tenant=tenant, gallery=gallery, run_id=None,
                                   vision_summary={"hero_image_ids": [img["id"]], "keeper_count": 1},
                                   flags=tenant_flags(tenant))
    conn.commit()
    return gallery, offer


def test_gallery_sales_opportunity_becomes_ready_after_delivery(conn, storage, settings):
    tenant = _tenant(conn, "Opportunity")
    gallery, offer = _ready_sales_gallery(conn, storage, settings, tenant)

    opp = gallery_sales_opportunity(conn, tenant["id"], gallery["id"])

    assert opp["status"] == "ready"
    assert opp["status_label"] == "Ready to sell"
    assert opp["offer_token"] == offer["token"]
    assert "Delivered" in opp["reason_line"]
    assert "Offer ready" in opp["reason_line"]


def test_gallery_sales_opportunity_blocks_incomplete_or_recent_gallery(conn, storage, settings):
    tenant = _tenant(conn, "Blocks")
    client = create_client(conn, tenant_id=tenant["id"], name="No Delivery", email="nod@example.com")
    project = create_project(conn, tenant_id=tenant["id"], name="No Delivery", client_id=client["id"])
    gallery = create_gallery(conn, tenant_id=tenant["id"], title="No Delivery")
    assign_gallery_to_project(conn, tenant["id"], gallery["id"], project["id"])
    img = add_image(conn, storage, tenant_id=tenant["id"], gallery_id=gallery["id"],
                    filename="a.jpg", fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")
    publish_gallery(conn, tenant["id"], gallery["id"])
    create_or_update_offer(conn, tenant=tenant, gallery=gallery, run_id=None,
                           vision_summary={"hero_image_ids": [img["id"]], "keeper_count": 1},
                           flags=tenant_flags(tenant))
    conn.commit()

    assert gallery_sales_opportunity(conn, tenant["id"], gallery["id"])["status"] == "deliver"

    enable_delivery(conn, tenant["id"], gallery["id"])
    conn.execute("INSERT INTO audit_log (tenant_id, actor, action, detail) VALUES (?, 'system', ?, ?)",
                 (tenant["id"], "campaign.email_sent", f"gallery #{gallery['id']} · nod@example.com · auto"))
    conn.commit()
    assert gallery_sales_opportunity(conn, tenant["id"], gallery["id"])["status"] == "cooldown"


def test_launch_gallery_sales_campaign_emails_and_respects_cooldown(conn, storage, settings):
    tenant = _tenant(conn, "Launch")
    gallery, offer = _ready_sales_gallery(conn, storage, settings, tenant, email="buyer@example.com")

    result = launch_gallery_sales_campaign(conn, settings, tenant=tenant, gallery_id=gallery["id"],
                                           headline="Weekend print sale", discount_pct=20, days=5)
    conn.commit()

    assert result["sent"] is True
    assert result["offer_url"].endswith(f"/{offer['token']}")
    assert get_active_campaign(conn, gallery["id"], tenant_id=tenant["id"])["discount_pct"] == 20
    outbox = list_emails(conn, tenant["id"], to_addr="buyer@example.com")
    assert len(outbox) == 1 and "20% off" in outbox[0]["subject"]

    end_campaign(conn, tenant["id"], gallery["id"])
    result2 = launch_gallery_sales_campaign(conn, settings, tenant=tenant, gallery_id=gallery["id"],
                                            headline="Another sale", discount_pct=20, days=5)
    conn.commit()

    assert result2["sent"] is False and result2["status"] == "cooldown"
    assert len(list_emails(conn, tenant["id"], to_addr="buyer@example.com")) == 1


def test_auto_gallery_sales_sweep_sends_only_ready_once(conn, storage, settings):
    ready = _tenant(conn, "Auto Ready")
    ready_gallery, _offer = _ready_sales_gallery(conn, storage, settings, ready, email="ready@example.com")
    blocked = _tenant(conn, "Auto Blocked")
    _ready_sales_gallery(conn, storage, settings, blocked, email="blocked@example.com")
    conn.execute("UPDATE galleries SET delivery_token = NULL WHERE tenant_id = ?", (blocked["id"],))
    conn.commit()

    assert send_gallery_sales_campaigns(conn, settings) == 1
    conn.commit()

    assert len(list_emails(conn, ready["id"], to_addr="ready@example.com")) == 1
    assert list_emails(conn, blocked["id"], to_addr="blocked@example.com") == []
    assert get_active_campaign(conn, ready_gallery["id"], tenant_id=ready["id"]) is not None
    assert send_gallery_sales_campaigns(conn, settings) == 0


def _setup_for_tenant(app, tenant_id, slug):
    """In the app DB: a published gallery with an offer, linked to a client w/ email."""
    conn = connect(app.state.settings.db_path)
    try:
        t = next(dict(r) for r in conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)))
        client = create_client(conn, tenant_id=tenant_id, name="Sarah", email="sarah@example.com")
        project = create_project(conn, tenant_id=tenant_id, name="Wedding", client_id=client["id"])
        g = create_gallery(conn, tenant_id=tenant_id, title="Wedding")
        assign_gallery_to_project(conn, tenant_id, g["id"], project["id"])
        img = add_image(conn, app.state.storage, tenant_id=tenant_id, gallery_id=g["id"],
                        filename="a.jpg", fileobj=io.BytesIO(b"jpg"), content_type="image/jpeg")
        publish_gallery(conn, tenant_id, g["id"])
        offer = create_or_update_offer(conn, tenant=t, gallery=dict(g), run_id=None,
                                       vision_summary={"hero_image_ids": [img["id"]], "keeper_count": 1},
                                       flags=tenant_flags(t))
        conn.commit()
        return g, offer
    finally:
        conn.close()


def _tenant_row(app):
    conn = connect(app.state.settings.db_path)
    try:
        r = conn.execute("SELECT id, slug FROM tenants LIMIT 1").fetchone()
        return r["id"], r["slug"]
    finally:
        conn.close()


def test_http_launch_discounts_offer_and_emails(client, app):
    login_owner(client, onboard_studio(client, email="owner@example.com"))
    tenant_id, slug = _tenant_row(app)
    g, offer = _setup_for_tenant(app, tenant_id, slug)
    offer_path = f"/s/{slug}/{offer['token']}"

    # full price before any sale (Signature Print Set is $120)
    assert "$120" in client.get(offer_path).text

    client.post(f"/galleries/{g['id']}/campaign",
                data={"headline": "Holiday sale", "discount_pct": "25", "days": "7"})
    page = client.get(offer_path).text
    assert "Holiday sale" in page and "25% off" in page
    assert "$90" in page  # $120 discounted 25%

    # the client was emailed the sale
    conn = connect(app.state.settings.db_path)
    try:
        outbox = list_emails(conn, tenant_id)
    finally:
        conn.close()
    assert any("off your prints" in m["subject"] for m in outbox)

    # ending the sale restores full price and drops the banner
    client.post(f"/galleries/{g['id']}/campaign/end")
    after = client.get(offer_path).text
    assert "Holiday sale" not in after and "$120" in after

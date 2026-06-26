"""The 'today' dashboard — needs-attention aggregation across the studio."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client, create_project
from hestia.dashboard import needs_attention
from hestia.db import connect
from hestia.delivery import enable_delivery
from hestia.galleries import create_gallery, publish_gallery
from hestia.invoices import create_invoice
from hestia.tenants import create_tenant


def test_needs_attention_empty(conn):
    t = create_tenant(conn, name="Quiet Studio", shoot_type="wedding")
    conn.commit()
    a = needs_attention(conn, t["id"])
    assert a["total"] == 0
    assert a["leads"] == [] and a["unpaid"] == [] and a["upcoming"] == [] and a["to_deliver"] == []


def test_needs_attention_aggregates(conn, settings):
    t = create_tenant(conn, name="Busy Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Cli", email="c@x.com")
    create_project(conn, tenant_id=t["id"], name="Fresh lead", client_id=c["id"],
                   shoot_type="wedding", status="lead")
    create_project(conn, tenant_id=t["id"], name="Already booked", client_id=c["id"],
                   shoot_type="wedding", status="booked")          # booked → not a lead
    create_invoice(conn, settings, tenant_id=t["id"], title="Deposit",
                   amount_cents=10000, client_id=c["id"])
    g = create_gallery(conn, tenant_id=t["id"], title="To deliver")
    publish_gallery(conn, t["id"], g["id"])                        # published, undelivered
    g2 = create_gallery(conn, tenant_id=t["id"], title="Done")
    publish_gallery(conn, t["id"], g2["id"])
    enable_delivery(conn, t["id"], g2["id"])                       # delivered → excluded
    conn.execute("INSERT INTO appointments (tenant_id, title, status, token, starts_at) "
                 "VALUES (?, 'Engagement shoot', 'confirmed', 'tok-future', datetime('now','+3 days'))",
                 (t["id"],))
    conn.execute("INSERT INTO appointments (tenant_id, title, status, token, starts_at) "
                 "VALUES (?, 'Old shoot', 'confirmed', 'tok-past', datetime('now','-3 days'))",
                 (t["id"],))                                       # past → excluded
    conn.commit()

    a = needs_attention(conn, t["id"])
    assert [x["name"] for x in a["leads"]] == ["Fresh lead"]
    assert len(a["unpaid"]) == 1 and a["unpaid"][0]["amount_display"] == "$100.00"
    assert [x["title"] for x in a["to_deliver"]] == ["To deliver"]
    assert [x["title"] for x in a["upcoming"]] == ["Engagement shoot"]
    assert a["total"] == 4


def test_dashboard_page_shows_attention(client, app):
    creds = onboard_studio(client, name="Dash Studio", email="dash@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Lead Client")
        create_project(conn, tenant_id=tid, name="New inquiry", client_id=c["id"],
                       shoot_type="wedding", status="lead")
        conn.commit()
    finally:
        conn.close()
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Needs attention" in page.text and "New inquiry" in page.text


def test_dashboard_all_clear_for_fresh_studio(client):
    creds = onboard_studio(client, name="Clean Studio", email="clean@example.com")
    login_owner(client, creds)
    assert "all caught up" in client.get("/dashboard").text

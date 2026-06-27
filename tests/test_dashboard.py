"""The 'today' dashboard — needs-attention aggregation across the studio."""

from conftest import login_owner, onboard_studio

from hestia.contracts import create_contract, send_contract
from hestia.crm import create_client, create_project
from hestia.dashboard import money_snapshot, needs_attention
from hestia.db import connect
from hestia.delivery import enable_delivery
from hestia.galleries import create_gallery, publish_gallery
from hestia.invoices import create_invoice
from hestia.questionnaires import create_questionnaire, send_questionnaire
from hestia.tenants import create_tenant


def test_needs_attention_empty(conn):
    t = create_tenant(conn, name="Quiet Studio", shoot_type="wedding")
    conn.commit()
    a = needs_attention(conn, t["id"])
    assert a["total"] == 0
    assert a["leads"] == [] and a["unpaid"] == [] and a["upcoming"] == [] and a["to_deliver"] == []
    assert a["awaiting_contract"] == [] and a["awaiting_questionnaire"] == []


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


def test_upcoming_excludes_unparseable_freetext_times(conn):
    t = create_tenant(conn, name="Freetext Studio", shoot_type="wedding")
    conn.execute("INSERT INTO appointments (tenant_id, title, status, token, starts_at) "
                 "VALUES (?, 'Real session', 'confirmed', 'tok-iso', datetime('now','+2 days'))",
                 (t["id"],))
    conn.execute("INSERT INTO appointments (tenant_id, title, status, token, starts_at) "
                 "VALUES (?, 'Vague session', 'confirmed', 'tok-text', 'sometime next week')",
                 (t["id"],))
    conn.commit()
    # a parseable timestamp is upcoming; free text yields NULL via datetime() and is
    # excluded (not shown as stale or mis-sorted by a string compare)
    assert [x["title"] for x in needs_attention(conn, t["id"])["upcoming"]] == ["Real session"]


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


def test_needs_attention_surfaces_awaiting_client_actions(conn):
    t = create_tenant(conn, name="Pending Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Pat")
    ct = create_contract(conn, tenant_id=t["id"], title="Wedding agreement", client_id=c["id"])
    send_contract(conn, t["id"], ct["id"])                         # sent → awaiting signature
    q = create_questionnaire(conn, tenant_id=t["id"], title="Wedding details",
                             prompts=["Venue?"], client_id=c["id"])
    send_questionnaire(conn, t["id"], q["id"])                     # sent → awaiting response
    # excluded: an unsent draft, a signed contract, and a completed questionnaire
    create_contract(conn, tenant_id=t["id"], title="Draft only")
    signed = create_contract(conn, tenant_id=t["id"], title="Already signed")
    send_contract(conn, t["id"], signed["id"])
    conn.execute("UPDATE contracts SET status='signed' WHERE id=?", (signed["id"],))
    done = create_questionnaire(conn, tenant_id=t["id"], title="Done form", prompts=["X?"])
    send_questionnaire(conn, t["id"], done["id"])
    conn.execute("UPDATE questionnaires SET status='completed' WHERE id=?", (done["id"],))
    conn.commit()

    a = needs_attention(conn, t["id"])
    assert [x["title"] for x in a["awaiting_contract"]] == ["Wedding agreement"]
    assert a["awaiting_contract"][0]["client_name"] == "Pat"
    assert [x["title"] for x in a["awaiting_questionnaire"]] == ["Wedding details"]
    assert a["total"] == 2                                         # only the two sent items


def test_awaiting_client_actions_are_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    ct = create_contract(conn, tenant_id=b["id"], title="B contract")
    send_contract(conn, b["id"], ct["id"])
    q = create_questionnaire(conn, tenant_id=b["id"], title="B form", prompts=["X?"])
    send_questionnaire(conn, b["id"], q["id"])
    conn.commit()
    res = needs_attention(conn, a["id"])                           # A sees none of B's
    assert res["awaiting_contract"] == [] and res["awaiting_questionnaire"] == []


def test_dashboard_page_shows_awaiting_signature(client, app):
    creds = onboard_studio(client, name="Sig Studio", email="sig@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        ct = create_contract(conn, tenant_id=tid, title="Please sign me")
        send_contract(conn, tid, ct["id"])
        conn.commit()
    finally:
        conn.close()
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Awaiting signature" in page.text and "Please sign me" in page.text


def test_money_snapshot_reports_month_revenue_and_outstanding(conn, settings):
    t = create_tenant(conn, name="Money Studio", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Cli")
    paid = create_invoice(conn, settings, tenant_id=t["id"], title="Paid",
                          amount_cents=300000, client_id=c["id"])
    conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?",
                 (paid["id"],))                                  # revenue this month
    sent = create_invoice(conn, settings, tenant_id=t["id"], title="Owed",
                          amount_cents=125000, client_id=c["id"])
    conn.execute("UPDATE invoices SET status='sent' WHERE id=?", (sent["id"],))  # outstanding
    conn.commit()

    snap = money_snapshot(conn, t["id"])
    assert snap["month"]["revenue_cents"] == 300000
    assert snap["month"]["profit_cents"] == 300000              # no expenses → profit = revenue
    assert snap["ar"]["outstanding_cents"] == 125000


def test_money_snapshot_is_tenant_scoped(conn, settings):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    paid = create_invoice(conn, settings, tenant_id=b["id"], title="B paid", amount_cents=500000)
    conn.execute("UPDATE invoices SET status='paid', paid_at=datetime('now') WHERE id=?",
                 (paid["id"],))
    conn.commit()
    snap = money_snapshot(conn, a["id"])                         # A sees none of B's money
    assert snap["month"]["revenue_cents"] == 0 and snap["ar"]["outstanding_cents"] == 0


def test_dashboard_page_shows_money_card(client, app):
    creds = onboard_studio(client, name="Snap Studio", email="snap@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Cli")
        sent = create_invoice(conn, app.state.settings, tenant_id=tid, title="Owed",
                              amount_cents=125000, client_id=c["id"])
        conn.execute("UPDATE invoices SET status='sent' WHERE id=?", (sent["id"],))
        conn.commit()
    finally:
        conn.close()
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Money" in page.text and "$1,250.00" in page.text     # outstanding A/R shown

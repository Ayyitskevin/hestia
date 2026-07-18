"""The 'today' dashboard — needs-attention aggregation across the studio."""

from io import BytesIO

from conftest import login_owner, onboard_studio

from hestia.booking import create_booking_type
from hestia.contracts import create_contract, send_contract
from hestia.crm import assign_gallery_to_project, create_client, create_project
from hestia.dashboard import (
    hot_leads,
    money_snapshot,
    needs_attention,
    setup_checklist,
    trial_cockpit,
)
from hestia.db import connect
from hestia.delivery import enable_delivery
from hestia.features import flags_for
from hestia.galleries import (
    add_image,
    create_gallery,
    gallery_count,
    publish_gallery,
    recent_galleries,
)
from hestia.invoices import create_invoice, send_invoice
from hestia.packages import create_package
from hestia.proposals import (
    accept_proposal,
    create_proposal,
    proposal_followups,
    record_proposal_view,
    send_proposal,
)
from hestia.questionnaires import create_questionnaire, send_questionnaire
from hestia.routes import web as web_routes
from hestia.sales import create_or_update_offer
from hestia.subscriptions import apply_plan, get_subscription
from hestia.tenants import create_tenant


def test_recent_galleries_are_bounded_ordered_and_tenant_scoped(conn):
    studio = create_tenant(conn, name="Recent Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Other Studio", shoot_type="portrait")
    empty = create_tenant(conn, name="Empty Studio", shoot_type="food")
    galleries = [
        create_gallery(conn, tenant_id=studio["id"], title=f"Gallery {index}")
        for index in range(8)
    ]
    for gallery in galleries:
        conn.execute(
            "UPDATE galleries SET created_at = '2030-01-01 12:00:00' WHERE id = ?",
            (gallery["id"],),
        )
    foreign = create_gallery(conn, tenant_id=other["id"], title="Foreign gallery")
    conn.commit()

    recent = recent_galleries(conn, studio["id"], limit=6)

    assert gallery_count(conn, studio["id"]) == 8
    assert gallery_count(conn, other["id"]) == 1
    assert gallery_count(conn, empty["id"]) == 0
    assert [gallery["id"] for gallery in recent] == [
        gallery["id"] for gallery in reversed(galleries[-6:])
    ]
    assert foreign["id"] not in {gallery["id"] for gallery in recent}
    assert recent_galleries(conn, empty["id"], limit=6) == []
    assert recent_galleries(conn, studio["id"], limit=-1) == []


def test_recent_galleries_preserve_dashboard_summary_semantics(conn, storage):
    studio = create_tenant(conn, name="Summary Studio", shoot_type="wedding")
    other = create_tenant(conn, name="Other Summary Studio", shoot_type="portrait")
    client = create_client(conn, tenant_id=studio["id"], name="Current Client")
    project = create_project(
        conn,
        tenant_id=studio["id"],
        name="Current project",
        client_id=client["id"],
    )
    linked = create_gallery(
        conn,
        tenant_id=studio["id"],
        title="Linked gallery",
        client_name="Legacy Client",
    )
    legacy = create_gallery(
        conn,
        tenant_id=studio["id"],
        title="Legacy gallery",
        client_name="Unlinked Client",
    )
    assert assign_gallery_to_project(conn, studio["id"], linked["id"], project["id"])
    add_image(
        conn,
        storage,
        tenant_id=studio["id"],
        gallery_id=linked["id"],
        filename="visible.jpg",
        fileobj=BytesIO(b"visible"),
        content_type="image/jpeg",
    )
    hidden = add_image(
        conn,
        storage,
        tenant_id=studio["id"],
        gallery_id=linked["id"],
        filename="hidden.jpg",
        fileobj=BytesIO(b"hidden"),
        content_type="image/jpeg",
    )
    conn.execute("UPDATE images SET hidden = 1 WHERE id = ?", (hidden["id"],))
    conn.execute(
        "INSERT INTO images "
        "(gallery_id, tenant_id, filename, storage_key, content_type, position) "
        "VALUES (?, ?, 'foreign.jpg', 'foreign/object.jpg', 'image/jpeg', 99)",
        (linked["id"], other["id"]),
    )
    conn.commit()

    by_id = {
        gallery["id"]: gallery
        for gallery in recent_galleries(conn, studio["id"], limit=6)
    }

    assert set(by_id[linked["id"]]) == {
        "id",
        "title",
        "client_name",
        "status",
        "image_count",
    }
    assert by_id[linked["id"]]["client_name"] == "Current Client"
    assert by_id[linked["id"]]["image_count"] == 2
    assert by_id[legacy["id"]]["client_name"] == "Unlinked Client"


def test_dashboard_gallery_summary_is_two_selects_without_n_plus_one(conn):
    studio = create_tenant(conn, name="Query Studio", shoot_type="wedding")
    for index in range(12):
        gallery = create_gallery(
            conn,
            tenant_id=studio["id"],
            title=f"Query gallery {index}",
        )
        conn.execute(
            "INSERT INTO images "
            "(gallery_id, tenant_id, filename, storage_key, content_type, position) "
            "VALUES (?, ?, ?, ?, 'image/jpeg', 0)",
            (gallery["id"], studio["id"], f"{index}.jpg", f"query/{index}.jpg"),
        )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    try:
        count = gallery_count(conn, studio["id"])
        recent = recent_galleries(conn, studio["id"], limit=6)
    finally:
        conn.set_trace_callback(None)

    selects = [
        sql for sql in statements if sql.lstrip().upper().startswith(("SELECT", "WITH"))
    ]
    normalized = [" ".join(sql.upper().split()) for sql in selects]
    assert count == 12
    assert len(recent) == 6
    assert [gallery["image_count"] for gallery in recent] == [1] * 6
    assert len(selects) == 2
    assert sum("LIMIT 6" in sql for sql in normalized) == 1
    assert all("ALBUMS" not in sql for sql in normalized)


def test_dashboard_shows_full_gallery_count_and_only_six_recent_titles(
    client,
    app,
    monkeypatch,
):
    credentials = onboard_studio(
        client,
        name="Gallery Dashboard Studio",
        email="gallery-dashboard@example.com",
    )
    login_owner(client, credentials)
    conn = connect(app.state.settings.db_path)
    try:
        tenant_id = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        titles = []
        for index in range(8):
            title = f"Dashboard gallery {index:02d}"
            gallery = create_gallery(conn, tenant_id=tenant_id, title=title)
            conn.execute(
                "UPDATE galleries SET created_at = ? WHERE id = ?",
                (f"2030-01-{index + 1:02d} 12:00:00", gallery["id"]),
            )
            titles.append(title)
        conn.commit()
    finally:
        conn.close()

    calls = []
    real_count = web_routes.gallery_count
    real_recent = web_routes.recent_galleries

    def observed_count(conn, observed_tenant_id):
        calls.append(("count", observed_tenant_id))
        return real_count(conn, observed_tenant_id)

    def observed_recent(conn, observed_tenant_id, *, limit):
        calls.append(("recent", observed_tenant_id, limit))
        return real_recent(conn, observed_tenant_id, limit=limit)

    def obsolete_full_hydration(*_args, **_kwargs):
        raise AssertionError("dashboard must not hydrate every gallery")

    monkeypatch.setattr(web_routes, "gallery_count", observed_count)
    monkeypatch.setattr(web_routes, "recent_galleries", observed_recent)
    monkeypatch.setattr(
        web_routes,
        "list_galleries",
        obsolete_full_hydration,
        raising=False,
    )

    page = client.get("/dashboard")

    assert page.status_code == 200
    assert calls == [("recent", tenant_id, 6), ("count", tenant_id)]
    assert '<span class="svc">8</span> <span class="url">galleries</span>' in page.text
    expected = list(reversed(titles[-6:]))
    positions = [page.text.index(title) for title in expected]
    assert positions == sorted(positions)
    assert all(title not in page.text for title in titles[:2])


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


def test_hot_leads_rank_sales_signals_and_explain_score(conn, settings):
    t = create_tenant(conn, name="Lead Intel", shoot_type="wedding")
    c_hot = create_client(conn, tenant_id=t["id"], name="Mina", email="mina@example.com")
    hot = create_project(
        conn,
        tenant_id=t["id"],
        name="Holiday mini lead",
        client_id=c_hot["id"],
        shoot_type="portrait",
        status="lead",
        event_date="2030-12-01",
        notes="They want a gift-ready gallery, cards, and a quick turnaround before the holidays.",
        lead_source="mini_session",
    )
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Mini retainer",
                         amount_cents=17500, client_id=c_hot["id"], project_id=hot["id"])
    send_invoice(conn, t["id"], inv["id"])
    conn.execute(
        "INSERT INTO appointments (tenant_id, client_id, project_id, title, kind, status, "
        "starts_at, duration_minutes, token) VALUES (?, ?, ?, 'Holiday mini', 'shoot', "
        "'confirmed', '2030-12-01 10:00', 20, 'hot-lead-appt')",
        (t["id"], c_hot["id"], hot["id"]),
    )

    c_cold = create_client(conn, tenant_id=t["id"], name="Cold", email="")
    cold = create_project(
        conn,
        tenant_id=t["id"],
        name="Old Instagram lead",
        client_id=c_cold["id"],
        shoot_type="portrait",
        status="lead",
        lead_source="Instagram",
    )
    conn.execute("UPDATE projects SET created_at = datetime('now', '-20 days') WHERE id = ?",
                 (cold["id"],))
    other = create_tenant(conn, name="Other Lead Intel", shoot_type="wedding")
    other_client = create_client(conn, tenant_id=other["id"], name="Other", email="other@example.com")
    create_project(conn, tenant_id=other["id"], name="Foreign hot lead",
                   client_id=other_client["id"], status="lead", lead_source="Referral")
    conn.commit()

    leads = hot_leads(conn, t["id"])

    assert [lead["name"] for lead in leads] == ["Holiday mini lead", "Old Instagram lead"]
    top = leads[0]
    assert top["priority"] == "Hot lead"
    assert top["next_action"] == "Collect retainer"
    assert top["intent_value"] == "$175.00"
    assert "Mini-session claim" in top["reasons"]
    assert "Confirmed session" in top["reasons"]
    assert "Retainer open" in top["reasons"]
    assert leads[1]["next_action"] == "Add an email before follow-up"
    assert leads[1]["action_href"] == f"/clients/{c_cold['id']}/edit"
    assert leads[1]["score"] < top["score"]


def test_hot_leads_excludes_non_leads_and_is_tenant_scoped(conn):
    a = create_tenant(conn, name="A Lead", shoot_type="wedding")
    b = create_tenant(conn, name="B Lead", shoot_type="wedding")
    ac = create_client(conn, tenant_id=a["id"], name="A Client", email="a@example.com")
    bc = create_client(conn, tenant_id=b["id"], name="B Client", email="b@example.com")
    create_project(conn, tenant_id=a["id"], name="A open lead", client_id=ac["id"],
                   status="lead", lead_source="Referral")
    create_project(conn, tenant_id=a["id"], name="A booked lead", client_id=ac["id"],
                   status="booked", lead_source="Referral")
    create_project(conn, tenant_id=b["id"], name="B open lead", client_id=bc["id"],
                   status="lead", lead_source="Referral")
    conn.commit()

    assert [lead["name"] for lead in hot_leads(conn, a["id"])] == ["A open lead"]


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
    assert f'href="/clients/{c["id"]}/edit">Add an email before follow-up</a>' in page.text


def test_dashboard_page_shows_lead_intelligence(client, app):
    creds = onboard_studio(client, name="Intel Studio", email="intel@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Mina", email="mina@example.com")
        lead = create_project(conn, tenant_id=tid, name="Mini-session buyer",
                              client_id=c["id"], shoot_type="portrait", status="lead",
                              lead_source="mini_session")
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Retainer",
                             amount_cents=20000, client_id=c["id"], project_id=lead["id"])
        send_invoice(conn, tid, inv["id"])
        conn.commit()
    finally:
        conn.close()
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Lead intelligence" in page.text
    assert "Mini-session buyer" in page.text
    assert "Hot lead" in page.text
    assert "Collect retainer" in page.text


def test_dashboard_page_shows_gallery_sales_queue(client, app):
    creds = onboard_studio(client, name="Sales Queue Studio", email="salesq@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        t = dict(conn.execute("SELECT * FROM tenants LIMIT 1").fetchone())
        buyer = create_client(conn, tenant_id=t["id"], name="Gallery Buyer", email="buyer@example.com")
        project = create_project(conn, tenant_id=t["id"], name="Delivered wedding", client_id=buyer["id"],
                                 shoot_type="wedding", status="delivered")
        gallery = create_gallery(conn, tenant_id=t["id"], title="Delivered Gallery")
        conn.commit()
        assign_gallery_to_project(conn, t["id"], gallery["id"], project["id"])
        img = add_image(conn, app.state.storage, tenant_id=t["id"], gallery_id=gallery["id"],
                        filename="a.jpg", fileobj=BytesIO(b"jpg"), content_type="image/jpeg")
        publish_gallery(conn, t["id"], gallery["id"])
        enable_delivery(conn, t["id"], gallery["id"])
        create_or_update_offer(conn, tenant=t, gallery=gallery, run_id=None,
                               vision_summary={"hero_image_ids": [img["id"]], "keeper_count": 1},
                               flags=flags_for(t["shoot_type"]))
        conn.commit()
    finally:
        conn.close()

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Gallery sales" in page.text
    assert "Delivered Gallery" in page.text
    assert "Launch 15% sale" in page.text


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


def test_proposal_followups_surface_conversion_bottlenecks(conn, settings):
    t = create_tenant(conn, name="Proposal Watch", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Pat", email="pat@example.com")
    pkg = create_package(conn, tenant_id=t["id"], name="Wedding Collection",
                         price_cents=400000, deposit_cents=100000)
    waiting = create_proposal(conn, settings, tenant_id=t["id"], package_id=pkg["id"],
                              title="Waiting proposal", client_id=c["id"])
    send_proposal(conn, t["id"], waiting["id"])
    accepted = create_proposal(conn, settings, tenant_id=t["id"], package_id=pkg["id"],
                               title="Accepted proposal", client_id=c["id"])
    send_proposal(conn, t["id"], accepted["id"])
    accept_proposal(conn, token=accepted["token"], accepted_name="Pat")
    done = create_proposal(conn, settings, tenant_id=t["id"], package_id=pkg["id"],
                           title="Booked proposal", client_id=c["id"])
    send_proposal(conn, t["id"], done["id"])
    accept_proposal(conn, token=done["token"], accepted_name="Pat")
    conn.execute("UPDATE contracts SET status='signed' WHERE id=?", (done["contract_id"],))
    conn.execute("UPDATE invoices SET status='paid' WHERE id=?", (done["invoice_id"],))
    conn.commit()

    followups = proposal_followups(conn, t["id"])
    assert [p["title"] for p in followups["awaiting_acceptance"]] == ["Waiting proposal"]
    assert followups["awaiting_acceptance"][0]["next_action"] == "Confirm delivery"
    record_proposal_view(conn, waiting["token"])
    followups = proposal_followups(conn, t["id"])
    assert followups["awaiting_acceptance"][0]["next_action"] == "Nudge acceptance"
    assert [p["title"] for p in followups["finish_booking"]] == ["Accepted proposal"]
    assert followups["finish_booking"][0]["followup_label"] == "Needs signature + payment"
    assert followups["finish_booking"][0]["next_action"] == "Finish booking"
    assert followups["open_value_cents"] == 200000

    attention = needs_attention(conn, t["id"])
    assert [p["title"] for p in attention["proposal_acceptance"]] == ["Waiting proposal"]
    assert [p["title"] for p in attention["proposal_booking"]] == ["Accepted proposal"]
    assert attention["total"] == 2


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


def test_dashboard_page_shows_proposal_followup(client, app):
    creds = onboard_studio(client, name="Prop Dash", email="propdash@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        c = create_client(conn, tenant_id=tid, name="Pat", email="pat@example.com")
        pkg = create_package(conn, tenant_id=tid, name="Wedding Collection",
                             price_cents=350000, deposit_cents=100000)
        p = create_proposal(conn, app.state.settings, tenant_id=tid, package_id=pkg["id"],
                            title="Follow this proposal", client_id=c["id"])
        send_proposal(conn, tid, p["id"])
        record_proposal_view(conn, p["token"])
        conn.commit()
    finally:
        conn.close()
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Proposal follow-up" in page.text and "Follow this proposal" in page.text
    assert "Nudge acceptance" in page.text
    assert "Proposal conversion" in page.text
    assert "Money stuck" in page.text and "$1,000.00" in page.text


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


def test_setup_checklist_tracks_activation(conn, settings, storage):
    t = create_tenant(conn, name="New Studio", shoot_type="wedding")
    fresh = setup_checklist(conn, t["id"], published=False)
    assert fresh["done"] == 0 and fresh["complete"] is False
    assert [s["stage"] for s in fresh["steps"]] == [
        "Preset", "Launch", "Book", "Client", "Deliver", "Sell", "Collect"
    ]
    assert [s["done"] for s in fresh["steps"]] == [False] * 7
    assert fresh["next"]["stage"] == "Preset"

    c = create_client(conn, tenant_id=t["id"], name="Cli")
    create_booking_type(conn, tenant_id=t["id"], title="Portrait session")
    create_project(conn, tenant_id=t["id"], name="P", client_id=c["id"], shoot_type="wedding",
                   status="lead")
    g = create_gallery(conn, tenant_id=t["id"], title="G")
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename="one.jpg",
              fileobj=BytesIO(b"x" * 32), content_type="image/jpeg")
    create_or_update_offer(conn, tenant=t, gallery=g, run_id=None,
                           vision_summary={"keeper_count": 1, "hero_image_ids": []},
                           flags=flags_for(t["shoot_type"]))
    inv = create_invoice(conn, settings, tenant_id=t["id"], title="Inv", amount_cents=1000)
    send_invoice(conn, t["id"], inv["id"])
    conn.commit()

    done = setup_checklist(conn, t["id"], published=True)
    assert done["done"] == 7 and done["complete"] is True and done["next"] is None


def test_dashboard_shows_get_started_for_fresh_studio(client):
    login_owner(client, onboard_studio(client, name="Fresh", email="fresh@example.com"))
    assert "Launch path" in client.get("/dashboard").text


def test_dashboard_hides_get_started_once_set_up(client, app):
    login_owner(client, onboard_studio(client, name="Setup", email="setup@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tid,)).fetchone()
        c = create_client(conn, tenant_id=tid, name="Cli")
        create_booking_type(conn, tenant_id=tid, title="Portrait session")
        create_project(conn, tenant_id=tid, name="P", client_id=c["id"], shoot_type="wedding",
                       status="lead")
        g = create_gallery(conn, tenant_id=tid, title="G")
        add_image(conn, app.state.storage, tenant_id=tid, gallery_id=g["id"], filename="one.jpg",
                  fileobj=BytesIO(b"x" * 32), content_type="image/jpeg")
        create_or_update_offer(conn, tenant=dict(tenant), gallery=g, run_id=None,
                               vision_summary={"keeper_count": 1, "hero_image_ids": []},
                               flags=flags_for("wedding"))
        inv = create_invoice(conn, app.state.settings, tenant_id=tid, title="Inv", amount_cents=1000)
        send_invoice(conn, tid, inv["id"])
        conn.commit()
    finally:
        conn.close()
    client.post("/settings/site", data={"headline": "Hi", "published": "1"})  # last step
    assert "Launch path" not in client.get("/dashboard").text


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


def test_trial_cockpit_summarizes_ready_trial(conn, settings):
    t = create_tenant(conn, name="Trial Studio", shoot_type="wedding")
    setup = setup_checklist(conn, t["id"], published=False)
    trial = trial_cockpit(t, None, settings, setup)
    assert trial["title"] == "14-day trial ready"
    assert trial["trial_days"] == 14
    assert trial["price"] == "$40/month"
    assert trial["billing_label"] == "Start 14-day trial"
    assert trial["next"]["stage"] == "Preset"


def test_trial_cockpit_summarizes_active_trial(conn, settings):
    t = create_tenant(conn, name="Active Trial", shoot_type="wedding")
    apply_plan(conn, t["id"], plan="studio", status="trialing", provider="mock")
    conn.commit()
    tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (t["id"],)).fetchone()
    setup = setup_checklist(conn, t["id"], published=False)
    trial = trial_cockpit(dict(tenant), get_subscription(conn, t["id"]), settings, setup)
    assert trial["title"] == "Trial active"
    assert "days left before $40/month" in trial["message"]
    assert trial["billing_label"] == "Manage billing"


def test_dashboard_shows_trial_cockpit_for_fresh_studio(client):
    login_owner(client, onboard_studio(client, name="Cockpit", email="cockpit@example.com"))
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Hosted studio cockpit" in page.text
    assert "14-day trial ready" in page.text
    assert "$40/month" in page.text
    assert "flat monthly plan" in page.text
    assert "Start 14-day trial" in page.text
    assert "Next: Choose a studio preset" in page.text


def test_dashboard_trial_cockpit_next_action_after_preset(client, app):
    creds = onboard_studio(client, name="Preset Done", email="presetdone@example.com")
    login_owner(client, creds)
    client.post("/onboarding", data={"preset": "wedding"})
    page = client.get("/dashboard")
    assert page.status_code == 200
    assert "Hosted studio cockpit" in page.text
    assert "Next: Publish studio site" in page.text

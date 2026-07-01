"""Mini-session drops — limited fixed-slot booking tied into the normal studio loop."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.crm import list_clients
from hestia.db import connect
from hestia.email import list_emails
from hestia.invoices import get_invoice_by_token
from hestia.mini_sessions import (
    add_mini_session_slots,
    claim_mini_session_slot,
    create_mini_session,
    get_mini_session,
    get_mini_session_by_slug,
    list_mini_session_slots,
    list_mini_sessions,
    list_published_mini_sessions,
    set_mini_session_status,
)
from hestia.scheduler import list_appointments
from hestia.tenants import create_tenant, get_tenant_by_slug, slugify


def _tenant(conn, name="Mini Studio"):
    tenant = create_tenant(conn, name=name, shoot_type="portrait")
    conn.commit()
    return tenant


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _publish(client):
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})


def test_create_publish_and_slot_summary(conn):
    tenant = _tenant(conn)
    drop = create_mini_session(
        conn,
        tenant_id=tenant["id"],
        title="Fall Family Minis",
        description="One day only",
        duration_minutes=20,
        price_cents=25000,
        deposit_cents=25000,
    )
    assert drop and drop["slug"] == "fall-family-minis"
    assert drop["slot_count"] == 0

    added = add_mini_session_slots(
        conn,
        tenant["id"],
        drop["id"],
        "2030-10-18 09:00\n2030-10-18T09:30\n2030-10-18 09:30\n",
    )
    assert added == 2
    set_mini_session_status(conn, tenant["id"], drop["id"], "published")

    fresh = get_mini_session(conn, tenant["id"], drop["id"])
    assert fresh["status"] == "published"
    assert fresh["slot_count"] == 2
    assert fresh["open_count"] == 2
    assert [d["id"] for d in list_published_mini_sessions(conn, tenant["id"])] == [drop["id"]]
    assert [d["id"] for d in list_mini_sessions(conn, tenant["id"])] == [drop["id"]]


def test_claim_mini_session_slot_creates_confirmed_booking(conn, settings):
    tenant = _tenant(conn)
    drop = create_mini_session(conn, tenant_id=tenant["id"], title="Headshot Minis",
                               duration_minutes=15)
    add_mini_session_slots(conn, tenant["id"], drop["id"], "2031-03-01 10:00")
    set_mini_session_status(conn, tenant["id"], drop["id"], "published")
    slot = list_mini_session_slots(conn, tenant["id"], drop["id"])[0]

    result = claim_mini_session_slot(
        conn,
        settings,
        tenant=tenant,
        drop=get_mini_session(conn, tenant["id"], drop["id"]),
        slot_id=slot["id"],
        name="Mina Client",
        email="mina@example.com",
    )
    conn.commit()

    assert result and result["invoice"] is None
    slots = list_mini_session_slots(conn, tenant["id"], drop["id"])
    assert slots[0]["status"] == "claimed"
    assert slots[0]["client_name"] == "Mina Client"
    appts = list_appointments(conn, tenant["id"])
    assert len(appts) == 1
    assert appts[0]["status"] == "confirmed"
    assert appts[0]["starts_at"] == "2031-03-01 10:00"
    assert result["project"]["lead_source"] == "mini_session"
    assert claim_mini_session_slot(
        conn,
        settings,
        tenant=tenant,
        drop=get_mini_session(conn, tenant["id"], drop["id"]),
        slot_id=slot["id"],
        name="Late Client",
        email="late@example.com",
    ) is None


def test_owner_creates_publishes_and_public_claims_spot(client, app):
    creds = onboard_studio(client, name="Drop Studio", email="mini_owner@example.com")
    login_owner(client, creds)
    _publish(client)
    slug = slugify("Drop Studio")

    assert "Mini-sessions" in client.get("/dashboard").text
    response = client.post(
        "/mini-sessions",
        data={
            "title": "Fall family minis",
            "description": "Twenty minutes at the park.",
            "duration_minutes": "20",
            "price": "250",
            "deposit": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    detail_path = response.headers["location"]
    assert detail_path.startswith("/mini-sessions/")
    client.post(f"{detail_path}/slots", data={"starts_at": "2030-10-18 09:00\n2030-10-18 09:30"})
    client.post(f"{detail_path}/publish")

    detail = client.get(detail_path).text
    assert "View public drop" in detail
    assert "2 open" in detail

    public_site = client.get(f"/studio/{slug}").text
    assert "Fall family minis" in public_site
    assert "Claim a mini-session spot" in public_site

    conn = connect(app.state.settings.db_path)
    try:
        tenant = get_tenant_by_slug(conn, slug)
        drop = get_mini_session_by_slug(conn, tenant["id"], "fall-family-minis")
        slot_id = list_mini_session_slots(conn, tenant["id"], drop["id"])[0]["id"]
    finally:
        conn.close()

    visitor = CSRFClient(app)
    page = visitor.get(f"/studio/{slug}/mini-sessions/fall-family-minis").text
    assert "2030-10-18 09:00" in page
    r = visitor.post(
        f"/studio/{slug}/mini-sessions/fall-family-minis",
        data={"slot_id": str(slot_id), "name": "Dana Drop", "email": "dana@example.com"},
    )
    assert r.status_code == 200
    assert "You're booked!" in r.text
    assert "Add to calendar" in r.text

    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        assert any(c["name"] == "Dana Drop" for c in list_clients(conn, tid))
        slots = list_mini_session_slots(conn, tid, drop["id"])
        assert slots[0]["status"] == "claimed"
        assert slots[0]["client_name"] == "Dana Drop"
        appts = list_appointments(conn, tid)
        assert len(appts) == 1
        assert appts[0]["status"] == "confirmed"
        subjects = [e["subject"] for e in list_emails(conn, tid)]
        assert any("Mini-session booked" in subject for subject in subjects)
    finally:
        conn.close()

    duplicate = visitor.post(
        f"/studio/{slug}/mini-sessions/fall-family-minis",
        data={"slot_id": str(slot_id), "name": "Late Drop", "email": "late@example.com"},
    )
    assert duplicate.status_code == 400
    assert "just claimed" in duplicate.text
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        assert len(list_appointments(conn, tid)) == 1
        assert all(c["name"] != "Late Drop" for c in list_clients(conn, tid))
    finally:
        conn.close()


def test_public_claim_with_deposit_redirects_to_pay(client, app):
    creds = onboard_studio(client, name="Paid Mini Studio", email="paid_mini@example.com")
    login_owner(client, creds)
    _publish(client)
    slug = slugify("Paid Mini Studio")
    response = client.post(
        "/mini-sessions",
        data={"title": "Holiday card minis", "duration_minutes": "15", "price": "175", "deposit": "175"},
        follow_redirects=False,
    )
    detail_path = response.headers["location"]
    client.post(f"{detail_path}/slots", data={"starts_at": "2030-11-12 13:00"})
    client.post(f"{detail_path}/publish")

    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        drop = get_mini_session_by_slug(conn, tid, "holiday-card-minis")
        slot = list_mini_session_slots(conn, tid, drop["id"])[0]
    finally:
        conn.close()

    visitor = CSRFClient(app)
    r = visitor.post(
        f"/studio/{slug}/mini-sessions/holiday-card-minis",
        data={"slot_id": str(slot["id"]), "name": "Paying Client", "email": "paymini@example.com"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/pay/")
    token = r.headers["location"].split("/pay/")[1]
    conn = connect(app.state.settings.db_path)
    try:
        invoice = get_invoice_by_token(conn, token)
        assert invoice["amount_cents"] == 17500
        assert invoice["status"] == "sent"
        claimed = list_mini_session_slots(conn, tid, drop["id"])[0]
        assert claimed["status"] == "claimed"
        assert claimed["invoice_id"] == invoice["id"]
    finally:
        conn.close()


def test_public_mini_session_is_gated_on_site_and_drop_publish(client, app):
    creds = onboard_studio(client, name="Gate Mini Studio", email="gate_mini@example.com")
    login_owner(client, creds)
    slug = slugify("Gate Mini Studio")
    response = client.post("/mini-sessions", data={"title": "Secret minis"}, follow_redirects=False)
    detail_path = response.headers["location"]
    client.post(f"{detail_path}/slots", data={"starts_at": "2030-09-01 09:00"})

    visitor = CSRFClient(app)
    assert "coming soon" in visitor.get(f"/studio/{slug}/mini-sessions/secret-minis").text.lower()
    _publish(client)
    assert visitor.get(f"/studio/{slug}/mini-sessions/secret-minis").status_code == 404
    client.post(f"{detail_path}/publish")
    assert visitor.get(f"/studio/{slug}/mini-sessions/secret-minis").status_code == 200

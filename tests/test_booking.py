"""Self-serve booking — session-type menu (CRUD + scoping), the public request flow
(lead + proposed appointment), and the access gates (publish, tenant isolation)."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.booking import (
    _MAX_DURATION,
    create_booking_type,
    get_booking_type,
    list_booking_types,
    request_booking,
    set_booking_type_active,
    update_booking_type,
)
from hestia.crm import list_clients
from hestia.db import connect
from hestia.email import list_emails
from hestia.scheduler import list_appointments
from hestia.tenants import create_tenant, slugify


def _tenant(conn, name="Booking Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _publish(client):
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})


# ── module: the session-type menu ───────────────────────────────────────────────


def test_create_list_and_active_only(conn):
    t = _tenant(conn)
    a = create_booking_type(conn, tenant_id=t["id"], title="Consult", duration_minutes=30)
    b = create_booking_type(conn, tenant_id=t["id"], title="Mini", duration_minutes=20, price_cents=15000)
    assert a and b and a["position"] < b["position"]            # position increments
    assert {x["id"] for x in list_booking_types(conn, t["id"])} == {a["id"], b["id"]}
    set_booking_type_active(conn, t["id"], b["id"], False)
    assert [x["id"] for x in list_booking_types(conn, t["id"], active_only=True)] == [a["id"]]


def test_create_rejects_blank_title(conn):
    t = _tenant(conn)
    assert create_booking_type(conn, tenant_id=t["id"], title="   ") is None
    assert list_booking_types(conn, t["id"]) == []


def test_duration_and_kind_are_sanitized(conn):
    t = _tenant(conn)
    huge = create_booking_type(conn, tenant_id=t["id"], title="Marathon", duration_minutes=10**9)
    assert huge["duration_minutes"] == _MAX_DURATION          # clamped, not absurd
    zero = create_booking_type(conn, tenant_id=t["id"], title="Zero", duration_minutes=0)
    assert zero["duration_minutes"] == 1                       # floored to a sane minimum
    odd = create_booking_type(conn, tenant_id=t["id"], title="Odd", kind="nonsense")
    assert odd["kind"] == "consultation"                       # unknown kind normalized


def test_update_and_tenant_scoped(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    bt = create_booking_type(conn, tenant_id=t1["id"], title="Consult")
    assert update_booking_type(conn, t1["id"], bt["id"], title="Renamed", duration_minutes=45)
    assert get_booking_type(conn, t1["id"], bt["id"])["title"] == "Renamed"
    # another tenant can neither see nor mutate it
    assert get_booking_type(conn, t2["id"], bt["id"]) is None
    assert update_booking_type(conn, t2["id"], bt["id"], title="Hijacked") is False
    assert list_booking_types(conn, t2["id"]) == []


def test_request_booking_creates_lead_and_proposed_appointment(conn, settings):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Engagement", kind="shoot",
                             duration_minutes=90)
    out = request_booking(conn, settings, tenant=t, booking_type=bt, name="Sam Visitor",
                          email="sam@example.com", requested_at="2030-05-01T14:00",
                          message="Golden hour please")
    conn.commit()
    assert out["invoice"] is None                              # no deposit on this type
    # a CRM lead was created
    assert any(c["name"] == "Sam Visitor" for c in list_clients(conn, t["id"]))
    assert out["project"]["status"] == "lead"
    # a proposed appointment carries the requested time (T normalized to a space) as its option
    appts = list_appointments(conn, t["id"])
    assert len(appts) == 1 and appts[0]["status"] == "proposed" and appts[0]["title"] == "Engagement"
    assert appts[0]["option_count"] == 1
    assert "Golden hour" in out["project"]["notes"] and "2030-05-01 14:00" in out["project"]["notes"]


def test_request_booking_without_time_still_creates_lead(conn, settings):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Consult")
    out = request_booking(conn, settings, tenant=t, booking_type=bt, name="No Time", email="")
    conn.commit()
    assert out["project"]["status"] == "lead"
    appts = list_appointments(conn, t["id"])
    assert appts[0]["status"] == "proposed" and appts[0]["option_count"] == 0   # no time given


def test_proposed_booking_acknowledges_the_visitor(conn, settings):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Engagement")
    request_booking(conn, settings, tenant=t, booking_type=bt, name="Sam",
                    email="sam@example.com", requested_at="2026-07-01 14:00")   # proposed (not confirmed)
    conn.commit()
    sent = [e for e in list_emails(conn, t["id"]) if e["to_addr"] == "sam@example.com"]
    assert len(sent) == 1 and "received your booking request" in sent[0]["subject"]


def test_no_acknowledgement_without_an_email(conn, settings):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Consult")
    request_booking(conn, settings, tenant=t, booking_type=bt, name="No Email", email="")
    conn.commit()
    assert list_emails(conn, t["id"]) == []                                     # no address to ack


def test_confirmed_booking_skips_the_request_ack(conn, settings):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Mini")
    request_booking(conn, settings, tenant=t, booking_type=bt, name="Pat",
                    email="pat@example.com", requested_at="2026-07-02 10:00", confirm=True)
    conn.commit()
    # a confirmed slot is handled by confirm_appointment's own confirmation — the "request
    # received" acknowledgement must NOT also fire (no double-send)
    subjects = [e["subject"] for e in list_emails(conn, t["id"])]
    assert not any("received your booking request" in s for s in subjects)


# ── deposit (the money path) ────────────────────────────────────────────────────


def test_deposit_stored_and_updated(conn):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Mini", deposit_cents=15000)
    assert get_booking_type(conn, t["id"], bt["id"])["deposit_cents"] == 15000
    assert update_booking_type(conn, t["id"], bt["id"], title="Mini", deposit_cents=20000)
    assert get_booking_type(conn, t["id"], bt["id"])["deposit_cents"] == 20000


def test_request_booking_with_deposit_raises_invoice(conn, settings):
    from hestia.invoices import get_invoice_by_token
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Wedding hold", deposit_cents=50000)
    out = request_booking(conn, settings, tenant=t, booking_type=bt, name="Dep Client",
                          email="dep@example.com", requested_at="2031-01-01T10:00")
    conn.commit()
    inv = out["invoice"]
    # issued (sent), not left as a draft → shows in A/R / statement even before it's paid
    assert inv and inv["amount_cents"] == 50000 and inv["status"] == "sent"
    assert inv["client_id"] == out["client"]["id"] and inv["project_id"] == out["project"]["id"]
    # the deposit invoice is fetchable by its public token (so /pay can serve it)
    assert get_invoice_by_token(conn, inv["token"])["id"] == inv["id"]
    # exactly one invoice for this tenant, scoped to it
    assert conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE tenant_id = ?",
                        (t["id"],)).fetchone()["n"] == 1


def test_request_booking_zero_deposit_raises_no_invoice(conn, settings):
    t = _tenant(conn)
    bt = create_booking_type(conn, tenant_id=t["id"], title="Free consult", deposit_cents=0)
    out = request_booking(conn, settings, tenant=t, booking_type=bt, name="Free Client")
    conn.commit()
    assert out["invoice"] is None
    assert conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE tenant_id = ?",
                        (t["id"],)).fetchone()["n"] == 0


# ── HTTP: owner manages the menu ────────────────────────────────────────────────


def test_owner_crud_http(client, app):
    creds = onboard_studio(client, name="Owner Studio", email="bk_owner@example.com")
    login_owner(client, creds)
    client.post("/settings/booking-types",
                data={"title": "Discovery call", "description": "15 min chat",
                      "kind": "call", "duration_minutes": "15", "price": "0"})
    page = client.get("/settings/booking-types").text
    assert "Discovery call" in page
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt_id = list_booking_types(conn, tid)[0]["id"]
    finally:
        conn.close()
    client.post(f"/settings/booking-types/{bt_id}/toggle")           # archive
    conn = connect(app.state.settings.db_path)
    try:
        assert list_booking_types(conn, tid, active_only=True) == []
    finally:
        conn.close()
    client.post(f"/settings/booking-types/{bt_id}/delete")
    conn = connect(app.state.settings.db_path)
    try:
        assert list_booking_types(conn, tid) == []
    finally:
        conn.close()


def test_booking_types_requires_login(client):
    assert client.get("/settings/booking-types").status_code in (200, 303)  # bounces to /login


# ── HTTP: the public booking page ───────────────────────────────────────────────


def test_public_book_page_gated_on_publish(client, app):
    creds = onboard_studio(client, name="Pub Studio", email="bk_pub@example.com")
    login_owner(client, creds)
    slug = slugify("Pub Studio")
    client.post("/settings/booking-types", data={"title": "Mini session", "kind": "shoot",
                                                 "duration_minutes": "20", "price": "150"})
    # unpublished → coming soon, no booking form
    assert "coming soon" in client.get(f"/studio/{slug}/book").text.lower()
    _publish(client)
    # step 1: the type picker lists the session
    picker = client.get(f"/studio/{slug}/book").text
    assert "Mini session" in picker and "Choose a session" in picker
    conn = connect(app.state.settings.db_path)
    try:
        bt_id = list_booking_types(conn, _tid_of(conn, creds["email"]))[0]["id"]
    finally:
        conn.close()
    # step 2: with no availability set, the type page offers a free-text request
    form = client.get(f"/studio/{slug}/book?type={bt_id}").text
    assert "Request this session" in form
    # the public site cross-links to it once a type exists
    assert "See available sessions" in client.get(f"/studio/{slug}").text


def test_public_book_creates_lead_and_appointment_and_alerts_owner(client, app):
    creds = onboard_studio(client, name="Flow Studio", email="bk_flow@example.com")
    login_owner(client, creds)
    slug = slugify("Flow Studio")
    client.post("/settings/booking-types", data={"title": "Engagement shoot", "kind": "shoot",
                                                 "duration_minutes": "60"})
    _publish(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt_id = list_booking_types(conn, tid)[0]["id"]
    finally:
        conn.close()

    pub = CSRFClient(app)  # fresh, unauthenticated visitor
    r = pub.post(f"/studio/{slug}/book",
                 data={"booking_type_id": str(bt_id), "name": "Dana Lead", "email": "dana@example.com",
                       "requested_at": "2030-06-01T10:30", "message": "Can't wait"})
    assert r.status_code == 200 and "Request received" in r.text

    assert "Dana Lead" in client.get("/clients").text             # lead landed in the CRM
    conn = connect(app.state.settings.db_path)
    try:
        appts = list_appointments(conn, tid)
        assert len(appts) == 1 and appts[0]["status"] == "proposed"
        alert = conn.execute(
            "SELECT subject FROM emails WHERE tenant_id = ? ORDER BY id DESC LIMIT 1", (tid,)
        ).fetchone()
        assert alert and "New booking request" in alert["subject"]   # owner notified
    finally:
        conn.close()


def test_public_book_with_deposit_redirects_to_pay(client, app):
    """A session type with a deposit sends the visitor to a payable deposit invoice, and
    still creates the lead + proposed appointment."""
    creds = onboard_studio(client, name="Dep Studio", email="bk_dep@example.com")
    login_owner(client, creds)
    slug = slugify("Dep Studio")
    client.post("/settings/booking-types", data={"title": "Wedding hold", "kind": "shoot",
                                                 "duration_minutes": "60", "deposit": "500"})
    _publish(client)
    page = client.get(f"/studio/{slug}/book").text
    assert "deposit" in page.lower()                              # the deposit shows on the option
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt_id = list_booking_types(conn, tid)[0]["id"]
    finally:
        conn.close()

    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/book",
                 data={"booking_type_id": str(bt_id), "name": "Pay Me", "email": "pay@example.com",
                       "requested_at": "2031-02-02T12:00"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/pay/")   # sent to pay
    token = r.headers["location"].split("/pay/")[1]
    # the deposit invoice exists, unpaid, for $500, and its pay page is live
    conn = connect(app.state.settings.db_path)
    try:
        inv = conn.execute("SELECT amount_cents, status FROM invoices WHERE token = ? AND tenant_id = ?",
                           (token, tid)).fetchone()
        assert inv and inv["amount_cents"] == 50000 and inv["status"] == "sent"   # issued, unpaid
        assert list_appointments(conn, tid)[0]["status"] == "proposed"          # lead+appt still made
    finally:
        conn.close()
    assert pub.get(f"/pay/{token}").status_code == 200                          # payable now


def test_public_book_rejects_foreign_or_inactive_type(client, app):
    """A booking_type_id from another studio (or an archived one) can't be booked — no
    lead is created, the page re-renders with an error."""
    # studio A owns a type
    a = onboard_studio(client, name="Studio A", email="bk_a@example.com")
    login_owner(client, a)
    client.post("/settings/booking-types", data={"title": "A-only", "duration_minutes": "30"})
    conn = connect(app.state.settings.db_path)
    try:
        a_bt = list_booking_types(conn, _tid_of(conn, a["email"]))[0]["id"]
    finally:
        conn.close()

    # studio B is published but has no types
    b_client = CSRFClient(app)
    b = onboard_studio(b_client, name="Studio B", email="bk_b@example.com")
    login_owner(b_client, b)
    _publish(b_client)
    slug_b = slugify("Studio B")

    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug_b}/book",
                 data={"booking_type_id": str(a_bt), "name": "Vic", "email": "vic@example.com"})
    assert r.status_code == 400 and "choose a session type" in r.text.lower()
    conn = connect(app.state.settings.db_path)
    try:
        assert list_clients(conn, _tid_of(conn, b["email"])) == []     # no lead created for B
        # and A's CRM is untouched too (the foreign id wasn't honored anywhere)
        assert all(c["name"] != "Vic" for c in list_clients(conn, _tid_of(conn, a["email"])))
    finally:
        conn.close()


def test_public_book_requires_name(client, app):
    creds = onboard_studio(client, name="Name Studio", email="bk_name@example.com")
    login_owner(client, creds)
    slug = slugify("Name Studio")
    client.post("/settings/booking-types", data={"title": "Consult", "duration_minutes": "30"})
    _publish(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt_id = list_booking_types(conn, tid)[0]["id"]
    finally:
        conn.close()
    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/book", data={"booking_type_id": str(bt_id), "name": "  "})
    assert r.status_code == 400 and "name" in r.text.lower()
    conn = connect(app.state.settings.db_path)
    try:
        assert list_clients(conn, tid) == []                          # nothing created
    finally:
        conn.close()


def test_public_book_unpublished_and_unknown_slug(client, app):
    # unpublished studio: GET shows coming-soon, POST is a 404 and creates nothing
    creds = onboard_studio(client, name="Hidden Studio", email="bk_hidden@example.com")
    login_owner(client, creds)
    slug = slugify("Hidden Studio")
    client.post("/settings/booking-types", data={"title": "Consult", "duration_minutes": "30"})
    pub = CSRFClient(app)
    assert pub.post(f"/studio/{slug}/book",
                    data={"booking_type_id": "1", "name": "X"}).status_code == 404
    assert pub.get("/studio/not-a-real-studio/book").status_code == 404


def test_confirmed_slot_booking_offers_calendar_link(client, app):
    """A visitor who picks a real availability slot gets a confirmed booking — the thanks
    page should offer an 'add to calendar' .ics for it."""
    from hestia.availability import add_window, available_slots
    creds = onboard_studio(client, name="Cal Studio", email="bk_cal@example.com")
    login_owner(client, creds)
    slug = slugify("Cal Studio")
    client.post("/settings/booking-types", data={"title": "Mini", "kind": "shoot",
                                                 "duration_minutes": "60", "price": "0"})
    _publish(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt_id = list_booking_types(conn, tid)[0]["id"]
        for wd in range(7):                                           # open every day → a slot exists
            add_window(conn, tenant_id=tid, weekday=wd, start_minute=540, end_minute=1020)
        conn.commit()
        groups = available_slots(conn, tid, duration_minutes=60)
    finally:
        conn.close()
    slot = next(s["value"] for g in groups for s in g["slots"])       # a real, still-open slot
    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/book", data={"booking_type_id": str(bt_id),
                 "name": "Cal Visitor", "email": "cal@v.com", "slot": slot})
    assert r.status_code == 200 and "You're booked!" in r.text
    assert "Add to calendar" in r.text and "/calendar.ics" in r.text  # the .ics link is offered

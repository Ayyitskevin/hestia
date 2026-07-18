"""Weekly availability — window CRUD/scoping, slot generation (step, future-only, conflict
exclusion), the booking-time re-check, and the public auto-confirm flow + double-booking guard."""

import datetime

from conftest import CSRFClient, login_owner, onboard_studio

from hestia import availability as availability_module
from hestia.availability import (
    add_window,
    available_slots,
    delete_window,
    has_availability,
    is_slot_open,
    list_windows,
)
from hestia.booking import list_booking_types
from hestia.db import connect
from hestia.scheduler import create_block, list_appointments
from hestia.tenants import create_tenant, set_booking_rules, slugify


def _tenant(conn, name="Avail Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _flat(groups):
    return [s["value"] for g in groups for s in g["slots"]]


# A fixed reference so slot generation is deterministic regardless of the wall clock.
_DAY = datetime.date(2030, 6, 3)               # any date; we anchor windows to its weekday
_WD = _DAY.weekday()
_MIDNIGHT = datetime.datetime.combine(_DAY, datetime.time(0, 0))


# ── window CRUD ─────────────────────────────────────────────────────────────────


def test_add_validates_and_lists(conn):
    t = _tenant(conn)
    assert add_window(conn, tenant_id=t["id"], weekday=2, start_minute=540, end_minute=1020)
    assert add_window(conn, tenant_id=t["id"], weekday=9, start_minute=0, end_minute=60) is None   # bad weekday
    assert add_window(conn, tenant_id=t["id"], weekday=2, start_minute=600, end_minute=600) is None  # empty range
    assert add_window(conn, tenant_id=t["id"], weekday=2, start_minute=600, end_minute=500) is None  # inverted
    w = list_windows(conn, t["id"])
    assert len(w) == 1 and w[0]["weekday_label"] == "Wednesday" and w[0]["start_label"] == "9:00 AM"


def test_windows_tenant_scoped(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    w = add_window(conn, tenant_id=t1["id"], weekday=_WD, start_minute=540, end_minute=660)
    assert has_availability(conn, t1["id"]) and not has_availability(conn, t2["id"])
    assert list_windows(conn, t2["id"]) == []
    delete_window(conn, t2["id"], w["id"])                       # foreign delete is a no-op
    assert has_availability(conn, t1["id"])
    delete_window(conn, t1["id"], w["id"])
    assert not has_availability(conn, t1["id"])


# ── slot generation ──────────────────────────────────────────────────────────────


def test_slots_stepped_by_duration(conn):
    t = _tenant(conn)
    add_window(conn, tenant_id=t["id"], weekday=_WD, start_minute=540, end_minute=660)   # 9:00–11:00
    slots = _flat(available_slots(conn, t["id"], duration_minutes=60, days=6,
                                  today=_DAY, now=_MIDNIGHT))
    assert slots == ["2030-06-03 09:00", "2030-06-03 10:00"]     # 11:00 wouldn't fit a 60-min session


def test_overlapping_windows_emit_each_exact_start_once(conn):
    t = _tenant(conn)
    for start, end in ((540, 660), (540, 660), (600, 720), (570, 690)):
        add_window(
            conn,
            tenant_id=t["id"],
            weekday=_WD,
            start_minute=start,
            end_minute=end,
        )

    slots = _flat(
        available_slots(
            conn,
            t["id"],
            duration_minutes=60,
            days=0,
            today=_DAY,
            now=_MIDNIGHT,
        )
    )

    assert slots == [
        "2030-06-03 09:00",
        "2030-06-03 09:30",
        "2030-06-03 10:00",
        "2030-06-03 10:30",
        "2030-06-03 11:00",
    ]


def test_duplicate_windows_do_not_consume_display_limit(conn):
    t = _tenant(conn)
    for weekday in range(7):
        for _ in range(2):
            add_window(
                conn,
                tenant_id=t["id"],
                weekday=weekday,
                start_minute=0,
                end_minute=24 * 60,
            )

    slots = _flat(
        available_slots(
            conn,
            t["id"],
            duration_minutes=60,
            days=14,
            today=_DAY,
            now=_MIDNIGHT - datetime.timedelta(minutes=1),
        )
    )

    assert len(slots) == 200
    assert len(set(slots)) == 200


def test_past_slots_excluded(conn):
    t = _tenant(conn)
    add_window(conn, tenant_id=t["id"], weekday=_WD, start_minute=540, end_minute=660)
    now = datetime.datetime.combine(_DAY, datetime.time(9, 30))   # 9:00 already passed
    slots = _flat(available_slots(conn, t["id"], duration_minutes=60, days=6, today=_DAY, now=now))
    assert slots == ["2030-06-03 10:00"]


def test_conflict_excludes_overlapping_slot(conn):
    t = _tenant(conn)
    add_window(conn, tenant_id=t["id"], weekday=_WD, start_minute=540, end_minute=660)   # 9–11
    create_block(conn, tenant_id=t["id"], title="Busy", starts_at="2030-06-03 10:00",
                 duration_minutes=60)                            # occupies 10:00–11:00
    conn.commit()
    slots = _flat(available_slots(conn, t["id"], duration_minutes=60, days=6, today=_DAY, now=_MIDNIGHT))
    assert slots == ["2030-06-03 09:00"]                         # 10:00 slot is taken


def test_slot_generation_does_not_hydrate_far_future_appointments(conn, monkeypatch):
    t = _tenant(conn)
    add_window(conn, tenant_id=t["id"], weekday=_WD, start_minute=540, end_minute=660)
    near = create_block(
        conn,
        tenant_id=t["id"],
        title="Near busy",
        starts_at="2030-06-03 10:00",
        duration_minutes=60,
    )
    conn.execute(
        "UPDATE appointments SET starts_at = '2030-06-03T10:00' WHERE id = ?",
        (near["id"],),
    )
    create_block(
        conn,
        tenant_id=t["id"],
        title="Far busy",
        starts_at="2099-01-01 10:00",
        duration_minutes=60,
    )
    conn.commit()
    parsed = []
    real_parse = availability_module._parse_dt

    def observed_parse(value):
        parsed.append(value)
        return real_parse(value)

    monkeypatch.setattr(availability_module, "_parse_dt", observed_parse)

    statements = []
    conn.set_trace_callback(statements.append)
    try:
        slots = _flat(
            available_slots(
                conn,
                t["id"],
                duration_minutes=60,
                days=6,
                today=_DAY,
                now=_MIDNIGHT,
            )
        )
    finally:
        conn.set_trace_callback(None)

    assert slots == ["2030-06-03 09:00"]
    assert "2030-06-03T10:00" in parsed
    assert "2099-01-01 10:00" not in parsed
    appointment_reads = [
        " ".join(statement.lower().split())
        for statement in statements
        if "select starts_at, duration_minutes from appointments" in statement.lower()
    ]
    assert len(appointment_reads) == 1
    assert "starts_at >= '2030-06-03'" in appointment_reads[0]
    assert "starts_at < '2030-06-10'" in appointment_reads[0]
    assert "date(starts_at)" not in appointment_reads[0]


def test_horizon_keeps_next_day_buffer_conflict(conn):
    t = _tenant(conn)
    add_window(
        conn,
        tenant_id=t["id"],
        weekday=_WD,
        start_minute=23 * 60,
        end_minute=24 * 60,
    )
    create_block(
        conn,
        tenant_id=t["id"],
        title="After-midnight busy",
        starts_at="2030-06-04 00:30",
        duration_minutes=60,
    )
    conn.commit()

    set_booking_rules(conn, t["id"], min_notice_hours=0, buffer_minutes=0)
    assert _flat(
        available_slots(
            conn,
            t["id"],
            duration_minutes=60,
            days=0,
            today=_DAY,
            now=_MIDNIGHT,
        )
    ) == ["2030-06-03 23:00"]

    set_booking_rules(conn, t["id"], min_notice_hours=0, buffer_minutes=60)
    assert _flat(
        available_slots(
            conn,
            t["id"],
            duration_minutes=60,
            days=0,
            today=_DAY,
            now=_MIDNIGHT,
        )
    ) == []


def test_is_slot_open_rechecks(conn):
    t = _tenant(conn)
    add_window(conn, tenant_id=t["id"], weekday=_WD, start_minute=540, end_minute=660)
    ok = {"duration_minutes": 60, "days": 6, "today": _DAY, "now": _MIDNIGHT}
    assert is_slot_open(conn, t["id"], slot="2030-06-03 09:00", **ok)
    assert not is_slot_open(conn, t["id"], slot="2030-06-03 09:30", **ok)   # off-grid
    assert not is_slot_open(conn, t["id"], slot="2030-06-03 18:00", **ok)   # outside window
    create_block(conn, tenant_id=t["id"], title="Busy", starts_at="2030-06-03 09:00",
                 duration_minutes=60)
    conn.commit()
    assert not is_slot_open(conn, t["id"], slot="2030-06-03 09:00", **ok)   # now taken


# ── HTTP: owner availability + the public auto-confirm flow ───────────────────────


def _open_all_week(client):
    """Open 08:00–20:00 every weekday so the next 14 days always have future slots."""
    for wd in range(7):
        client.post("/settings/availability", data={"weekday": str(wd), "start": "08:00", "end": "20:00"})


def _first_slot(app, tid, duration):
    conn = connect(app.state.settings.db_path)
    try:
        groups = available_slots(conn, tid, duration_minutes=duration)
    finally:
        conn.close()
    return groups[0]["slots"][0]["value"] if groups and groups[0]["slots"] else None


def test_owner_add_and_delete_availability_http(client, app):
    creds = onboard_studio(client, name="Hours Studio", email="av_owner@example.com")
    login_owner(client, creds)
    client.post("/settings/availability", data={"weekday": "1", "start": "09:00", "end": "17:00"})
    page = client.get("/settings/booking-types").text
    assert "Tuesday" in page and "9:00 AM" in page
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        wid = list_windows(conn, tid)[0]["id"]
    finally:
        conn.close()
    client.post(f"/settings/availability/{wid}/delete")
    conn = connect(app.state.settings.db_path)
    try:
        assert list_windows(conn, tid) == []
    finally:
        conn.close()


def test_public_book_auto_confirms_a_slot(client, app):
    creds = onboard_studio(client, name="Auto Studio", email="av_auto@example.com")
    login_owner(client, creds)
    slug = slugify("Auto Studio")
    client.post("/settings/booking-types", data={"title": "Portrait", "kind": "shoot",
                                                 "duration_minutes": "60"})
    _open_all_week(client)
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt = list_booking_types(conn, tid)[0]
    finally:
        conn.close()
    # the type page shows real slots
    assert "Pick a time" in client.get(f"/studio/{slug}/book?type={bt['id']}").text

    slot = _first_slot(app, tid, bt["duration_minutes"])
    assert slot
    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/book",
                 data={"booking_type_id": str(bt["id"]), "name": "Auto Client",
                       "email": "auto@example.com", "slot": slot})
    assert r.status_code == 200 and "booked" in r.text.lower()        # confirmed, not just requested
    conn = connect(app.state.settings.db_path)
    try:
        appts = list_appointments(conn, tid)
        assert len(appts) == 1 and appts[0]["status"] == "confirmed"   # auto-confirmed
        assert appts[0]["starts_at"] == slot
    finally:
        conn.close()


def test_public_book_double_booking_is_rejected(client, app):
    """The booking-time re-check: a slot taken by one visitor can't be booked again."""
    creds = onboard_studio(client, name="Dbl Studio", email="av_dbl@example.com")
    login_owner(client, creds)
    slug = slugify("Dbl Studio")
    client.post("/settings/booking-types", data={"title": "Consult", "duration_minutes": "60"})
    _open_all_week(client)
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt = list_booking_types(conn, tid)[0]
    finally:
        conn.close()
    slot = _first_slot(app, tid, bt["duration_minutes"])

    pub = CSRFClient(app)
    first = pub.post(f"/studio/{slug}/book",
                     data={"booking_type_id": str(bt["id"]), "name": "First", "slot": slot})
    assert first.status_code == 200
    # a second visitor tries the same slot → rejected, no second confirmed session at that time
    pub2 = CSRFClient(app)
    second = pub2.post(f"/studio/{slug}/book",
                       data={"booking_type_id": str(bt["id"]), "name": "Second", "slot": slot})
    assert second.status_code == 400 and "no longer available" in second.text.lower()
    conn = connect(app.state.settings.db_path)
    try:
        booked = [a for a in list_appointments(conn, tid) if a["starts_at"] == slot
                  and a["status"] == "confirmed"]
        assert len(booked) == 1                                       # exactly one booking at that slot
    finally:
        conn.close()


def test_public_book_without_availability_stays_proposed(client, app):
    """No open hours set → the free-text request path (proposed), unchanged from before."""
    creds = onboard_studio(client, name="Req Studio", email="av_req@example.com")
    login_owner(client, creds)
    slug = slugify("Req Studio")
    client.post("/settings/booking-types", data={"title": "Consult", "duration_minutes": "30"})
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt = list_booking_types(conn, tid)[0]
    finally:
        conn.close()
    # the type page offers a free-text time (no slot picker)
    assert "Preferred date" in client.get(f"/studio/{slug}/book?type={bt['id']}").text
    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/book",
                 data={"booking_type_id": str(bt["id"]), "name": "Req Client",
                       "requested_at": "2031-03-03T14:00"})
    assert r.status_code == 200
    conn = connect(app.state.settings.db_path)
    try:
        assert list_appointments(conn, tid)[0]["status"] == "proposed"   # owner still confirms
    finally:
        conn.close()


def test_public_book_availability_with_deposit_redirects_to_pay(client, app):
    """Availability + deposit: picking a slot confirms it AND sends the visitor to pay."""
    creds = onboard_studio(client, name="AD Studio", email="av_dep@example.com")
    login_owner(client, creds)
    slug = slugify("AD Studio")
    client.post("/settings/booking-types", data={"title": "Wedding", "kind": "shoot",
                                                 "duration_minutes": "60", "deposit": "400"})
    _open_all_week(client)
    client.post("/settings/site", data={"headline": "x", "about": "y", "contact_email": "",
                                        "published": "1"})
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        bt = list_booking_types(conn, tid)[0]
    finally:
        conn.close()
    slot = _first_slot(app, tid, bt["duration_minutes"])
    pub = CSRFClient(app)
    r = pub.post(f"/studio/{slug}/book",
                 data={"booking_type_id": str(bt["id"]), "name": "Dep Client", "slot": slot},
                 follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/pay/")
    conn = connect(app.state.settings.db_path)
    try:
        appts = list_appointments(conn, tid)
        assert appts[0]["status"] == "confirmed"                     # held, even though deposit unpaid
        token = r.headers["location"].split("/pay/")[1]
        inv = conn.execute("SELECT amount_cents, status FROM invoices WHERE token=? AND tenant_id=?",
                           (token, tid)).fetchone()
        assert inv["amount_cents"] == 40000 and inv["status"] == "sent"
    finally:
        conn.close()

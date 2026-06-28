"""Booking guardrails — minimum notice and buffer applied to slot generation, plus the
owner save route. (Defaults of 0 leave slot generation unchanged — covered in test_availability.)"""

import datetime

from conftest import login_owner, onboard_studio

from hestia.availability import add_window, available_slots
from hestia.db import connect
from hestia.scheduler import create_block
from hestia.tenants import create_tenant, get_tenant, set_booking_rules

_DAY = datetime.date(2030, 6, 3)
_WD = _DAY.weekday()
_MIDNIGHT = datetime.datetime.combine(_DAY, datetime.time(0, 0))


def _tenant(conn, name="Rules Studio"):
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


def test_min_notice_excludes_near_slots(conn):
    t = _tenant(conn)
    for wd in range(7):                                         # open every day so dates are comparable
        add_window(conn, tenant_id=t["id"], weekday=wd, start_minute=540, end_minute=1020)
    set_booking_rules(conn, t["id"], min_notice_hours=48, buffer_minutes=0)
    conn.commit()
    dates = {v.split(" ")[0] for v in _flat(
        available_slots(conn, t["id"], duration_minutes=60, days=6, today=_DAY, now=_MIDNIGHT))}
    assert _DAY.isoformat() not in dates                        # today: inside the 48h window
    assert (_DAY + datetime.timedelta(days=1)).isoformat() not in dates
    assert (_DAY + datetime.timedelta(days=2)).isoformat() in dates   # first day past the notice


def test_buffer_keeps_slots_clear_of_sessions(conn):
    t = _tenant(conn)
    add_window(conn, tenant_id=t["id"], weekday=_WD, start_minute=540, end_minute=720)   # 9–12
    create_block(conn, tenant_id=t["id"], title="Busy", starts_at="2030-06-03 10:00",
                 duration_minutes=60)                          # occupies 10:00–11:00
    conn.commit()
    # no buffer: adjacent slots are fine — 9:00 (ends 10:00) and 11:00 (starts 11:00) stay
    set_booking_rules(conn, t["id"], min_notice_hours=0, buffer_minutes=0)
    assert _flat(available_slots(conn, t["id"], duration_minutes=60, days=6,
                                 today=_DAY, now=_MIDNIGHT)) == ["2030-06-03 09:00", "2030-06-03 11:00"]
    # 30-min buffer pads the session to 9:30–11:30, so both neighbours are now too close
    set_booking_rules(conn, t["id"], min_notice_hours=0, buffer_minutes=30)
    assert _flat(available_slots(conn, t["id"], duration_minutes=60, days=6,
                                 today=_DAY, now=_MIDNIGHT)) == []


def test_set_booking_rules_clamps(conn):
    t = _tenant(conn)
    set_booking_rules(conn, t["id"], min_notice_hours=-5, buffer_minutes=999999)
    row = get_tenant(conn, t["id"])
    assert row["booking_min_notice_hours"] == 0 and row["booking_buffer_minutes"] == 24 * 60


def test_owner_saves_rules_http(client, app):
    creds = onboard_studio(client, email="rules_owner@example.com")
    login_owner(client, creds)
    client.post("/settings/booking-rules", data={"min_notice_hours": "24", "buffer_minutes": "30"})
    page = client.get("/settings/booking-types").text
    assert 'value="24"' in page and 'value="30"' in page       # the saved values render back
    conn = connect(app.state.settings.db_path)
    try:
        row = get_tenant(conn, _tid_of(conn, creds["email"]))
        assert row["booking_min_notice_hours"] == 24 and row["booking_buffer_minutes"] == 30
    finally:
        conn.close()

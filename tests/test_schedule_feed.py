"""Schedule .ics feed — a subscribe-able calendar of the studio's confirmed sessions
(recent + upcoming in-window), tenant-scoped, served from a literal route that wins
over /{appt_id}."""

import datetime

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.scheduler import (
    appointment_ics,
    create_appointment,
    get_appointment,
    schedule_ics,
)
from hestia.tenants import create_tenant


def _future(days: int, hhmm: str = "10:00") -> str:
    d = datetime.date.today() + datetime.timedelta(days=days)
    return f"{d.isoformat()} {hhmm}"


def _confirmed(conn, tid, *, title, when, client_id=None, location=""):
    a = create_appointment(conn, tenant_id=tid, title=title, options=["x"],
                           client_id=client_id, location=location)
    conn.execute("UPDATE appointments SET status='confirmed', starts_at=? WHERE id=?", (when, a["id"]))
    return a


def test_schedule_ics_includes_confirmed_in_window(conn):
    t = create_tenant(conn, name="Feed", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    _confirmed(conn, t["id"], title="Engagement", when=_future(3, "14:00"), client_id=c["id"],
               location="Park")
    _confirmed(conn, t["id"], title="Wedding", when=_future(20, "11:00"))
    # excluded: still-proposed, beyond the window (future + long past), unparseable
    create_appointment(conn, tenant_id=t["id"], title="Proposed", options=[_future(5)])
    _confirmed(conn, t["id"], title="FarOff", when=_future(200))
    _confirmed(conn, t["id"], title="LongPast", when=_future(-60))
    _confirmed(conn, t["id"], title="Vague", when="sometime soon")
    conn.commit()

    ics = schedule_ics(conn, t["id"])
    assert ics.startswith("BEGIN:VCALENDAR") and ics.rstrip().endswith("END:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == 2
    assert "SUMMARY:Engagement" in ics and "SUMMARY:Wedding" in ics and "LOCATION:Park" in ics
    for excluded in ("Proposed", "FarOff", "LongPast", "Vague"):
        assert excluded not in ics


def test_schedule_ics_is_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    _confirmed(conn, b["id"], title="B-shoot", when=_future(2))
    conn.commit()
    ics = schedule_ics(conn, a["id"])                            # A sees none of B's
    assert "BEGIN:VEVENT" not in ics and "B-shoot" not in ics


def test_empty_feed_is_still_valid(conn):
    t = create_tenant(conn, name="Quiet", shoot_type="wedding")
    conn.commit()
    ics = schedule_ics(conn, t["id"])
    assert ics.startswith("BEGIN:VCALENDAR") and "END:VCALENDAR" in ics and "BEGIN:VEVENT" not in ics


def test_appointment_ics_still_single_event(conn):
    """The refactor keeps the single-appointment .ics intact."""
    t = create_tenant(conn, name="One", shoot_type="wedding")
    a = _confirmed(conn, t["id"], title="Solo", when="2030-07-15 14:00")
    ics = appointment_ics(conn, get_appointment(conn, t["id"], a["id"]))
    assert ics.count("BEGIN:VEVENT") == 1 and "SUMMARY:Solo" in ics


def test_http_schedule_feed(client, app):
    login_owner(client, onboard_studio(client, email="feed@example.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        _confirmed(conn, tid, title="ShootDay", when=_future(4, "15:30"))
        conn.commit()
    finally:
        conn.close()
    r = client.get("/schedule/calendar.ics")
    assert r.status_code == 200                                 # literal route wins over /{appt_id}
    assert r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "SUMMARY:ShootDay" in r.text


def test_http_schedule_feed_requires_login(client):
    assert client.get("/schedule/calendar.ics", follow_redirects=False).status_code == 303

"""Appointment .ics — an 'Add to calendar' download for confirmed sessions, plus
the calendar link carried in the confirmation/reminder email."""

import datetime

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.db import connect
from hestia.email import list_emails
from hestia.scheduler import (
    _notify,
    appointment_ics,
    create_appointment,
    get_appointment,
)
from hestia.tenants import create_tenant


def _future(days: int = 14, hhmm: str = "14:00") -> str:
    d = datetime.date.today() + datetime.timedelta(days=days)
    return f"{d.isoformat()} {hhmm}"


def _confirmed(conn, tenant_id, *, title="Engagement shoot", when=None, duration=60,
               location="", client_id=None):
    appt = create_appointment(conn, tenant_id=tenant_id, title=title, options=["x"],
                              location=location, duration_minutes=duration, client_id=client_id)
    conn.execute("UPDATE appointments SET status='confirmed', starts_at=? WHERE id=?",
                 (when or _future(), appt["id"]))
    return get_appointment(conn, tenant_id, appt["id"])


# ── ICS generation (unit) ────────────────────────────────────────────────────


def test_ics_has_event_with_correct_floating_times(conn):
    t = create_tenant(conn, name="Cal", shoot_type="wedding")
    appt = _confirmed(conn, t["id"], when="2026-07-15 14:00", duration=90, location="Central Park")
    ics = appointment_ics(conn, appt)
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.rstrip().endswith("END:VCALENDAR")
    assert "BEGIN:VEVENT\r\n" in ics and "END:VEVENT\r\n" in ics
    assert "DTSTART:20260715T140000" in ics            # floating local time, exactly as typed
    assert "DTEND:20260715T153000" in ics              # start + 90 minutes
    assert "SUMMARY:Engagement shoot" in ics
    assert "LOCATION:Central Park" in ics
    assert f"UID:hestia-appt-{appt['id']}@hestia" in ics and "DTSTAMP:" in ics


def test_ics_none_for_proposed_and_unparseable(conn):
    t = create_tenant(conn, name="Cal2", shoot_type="wedding")
    proposed = create_appointment(conn, tenant_id=t["id"], title="Maybe", options=["x"])
    assert appointment_ics(conn, get_appointment(conn, t["id"], proposed["id"])) is None
    bad = _confirmed(conn, t["id"], when="whenever works for you")   # free-text, unplaceable
    assert appointment_ics(conn, bad) is None


def test_ics_escapes_special_chars(conn):
    t = create_tenant(conn, name="Cal3", shoot_type="wedding")
    appt = _confirmed(conn, t["id"], title="Smith, Jones; the album", location="A,B")
    ics = appointment_ics(conn, appt)
    assert "SUMMARY:Smith\\, Jones\\; the album" in ics
    assert "LOCATION:A\\,B" in ics


# ── HTTP routes ──────────────────────────────────────────────────────────────


def test_public_ics_route_serves_confirmed(client, app):
    onboard_studio(client, email="cal@example.com")     # public route — no login needed
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        token = _confirmed(conn, tid, when=_future(10, "09:30"))["token"]
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/book/{token}/calendar.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "BEGIN:VEVENT" in r.text and "DTSTART:" in r.text


def test_public_ics_404_for_proposed(client, app):
    onboard_studio(client, email="cal4@example.com")
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        token = create_appointment(conn, tenant_id=tid, title="Maybe", options=[_future()])["token"]
        conn.commit()
    finally:
        conn.close()
    assert client.get(f"/book/{token}/calendar.ics").status_code == 404


def test_owner_ics_route_and_tenant_scope(client, app):
    login_owner(client, onboard_studio(client, email="owner@cal.com"))
    conn = connect(app.state.settings.db_path)
    try:
        tid = conn.execute("SELECT id FROM tenants LIMIT 1").fetchone()["id"]
        mine_id = _confirmed(conn, tid, when=_future(5, "11:00"))["id"]
        other = create_tenant(conn, name="Other Studio", shoot_type="other")  # a different studio
        theirs_id = _confirmed(conn, other["id"], when=_future(5, "12:00"))["id"]
        conn.commit()
    finally:
        conn.close()
    r = client.get(f"/schedule/{mine_id}/calendar.ics")
    assert r.status_code == 200 and "BEGIN:VEVENT" in r.text
    # cross-tenant id is scoped out → redirect back, no foreign session leaked as .ics
    blocked = client.get(f"/schedule/{theirs_id}/calendar.ics", follow_redirects=False)
    assert blocked.status_code == 303


def test_owner_ics_requires_login(client):
    assert client.get("/schedule/1/calendar.ics", follow_redirects=False).status_code == 303


# ── email integration ────────────────────────────────────────────────────────


def test_confirmation_email_includes_calendar_link(conn, settings):
    t = create_tenant(conn, name="MailCal", shoot_type="wedding")
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@cal.com")
    appt = _confirmed(conn, t["id"], when=_future(), client_id=c["id"])
    conn.commit()
    _notify(settings, {"appointment_id": appt["id"], "kind": "confirm"})
    body = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sarah@cal.com"][0]["body"]
    assert f"/book/{appt['token']}/calendar.ics" in body

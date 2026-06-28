"""Client self-reschedule — the move (module) and the public /book/{token}/reschedule flow,
including the open-slot picker, the double-booking guard, and the no-availability fallback."""

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.availability import available_slots
from hestia.crm import create_client
from hestia.db import connect
from hestia.scheduler import (
    confirm_appointment,
    create_appointment,
    get_appointment,
    reschedule_by_token,
)
from hestia.tenants import create_tenant


def _tenant(conn, name="Reschedule Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _tid_of(conn, email):
    return conn.execute(
        "SELECT t.id FROM tenants t JOIN users u ON u.tenant_id = t.id WHERE u.email = ?",
        (email,),
    ).fetchone()["id"]


def _confirmed(conn, tenant_id, *, at="2031-01-01 10:00", client_id=None, dur=60):
    a = create_appointment(conn, tenant_id=tenant_id, title="Session", options=[at],
                           client_id=client_id, duration_minutes=dur)
    confirm_appointment(conn, tenant_id, a["id"], at)
    return get_appointment(conn, tenant_id, a["id"])


def _open_all_week(client):
    for wd in range(7):
        client.post("/settings/availability",
                    data={"weekday": str(wd), "start": "08:00", "end": "20:00"})


def _first_slot(app, tid, dur=60):
    conn = connect(app.state.settings.db_path)
    try:
        groups = available_slots(conn, tid, duration_minutes=dur)
    finally:
        conn.close()
    return groups[0]["slots"][0]["value"] if groups and groups[0]["slots"] else None


# ── module ───────────────────────────────────────────────────────────────────


def test_reschedule_moves_confirmed(conn, settings):
    t = _tenant(conn)
    a = _confirmed(conn, t["id"])
    conn.commit()
    assert reschedule_by_token(conn, settings, token=a["token"], new_slot="2031-02-02T14:00")
    fresh = get_appointment(conn, t["id"], a["id"])
    assert fresh["status"] == "confirmed" and fresh["starts_at"] == "2031-02-02 14:00"   # T normalized


def test_reschedule_noop_on_canceled(conn, settings):
    t = _tenant(conn)
    a = _confirmed(conn, t["id"])
    conn.execute("UPDATE appointments SET status='canceled' WHERE id=?", (a["id"],))
    conn.commit()
    assert reschedule_by_token(conn, settings, token=a["token"], new_slot="2031-02-02 14:00") is False
    assert get_appointment(conn, t["id"], a["id"])["status"] == "canceled"


def test_reschedule_unknown_token(conn, settings):
    assert reschedule_by_token(conn, settings, token="nope", new_slot="2031-02-02 14:00") is False


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_http_reschedule_flow(client, app):
    creds = onboard_studio(client, email="resc_flow@example.com")
    login_owner(client, creds)
    _open_all_week(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        c = create_client(conn, tenant_id=tid, name="Resa", email="resa@example.com")
        a = _confirmed(conn, tid, client_id=c["id"])
        conn.commit()
        tok, aid = a["token"], a["id"]
    finally:
        conn.close()
    slot = _first_slot(app, tid)
    assert slot

    assert "Reschedule" in client.get(f"/book/{tok}").text          # offered on the booking page
    pub = CSRFClient(app)
    assert "New time" in pub.get(f"/book/{tok}/reschedule").text     # the slot picker
    r = pub.post(f"/book/{tok}/reschedule", data={"slot": slot}, follow_redirects=False)
    assert r.status_code == 303
    conn = connect(app.state.settings.db_path)
    try:
        moved = get_appointment(conn, tid, aid)
        assert moved["starts_at"] == slot and moved["status"] == "confirmed"
    finally:
        conn.close()


def test_http_reschedule_rejects_taken_slot(client, app):
    creds = onboard_studio(client, email="resc_taken@example.com")
    login_owner(client, creds)
    _open_all_week(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        a = _confirmed(conn, tid)                                    # the session to move
        conn.commit()
        tok, aid = a["token"], a["id"]
    finally:
        conn.close()
    slot = _first_slot(app, tid)
    # occupy that slot with another confirmed session
    conn = connect(app.state.settings.db_path)
    try:
        _confirmed(conn, tid, at=slot)
        conn.commit()
    finally:
        conn.close()

    pub = CSRFClient(app)
    r = pub.post(f"/book/{tok}/reschedule", data={"slot": slot})
    assert r.status_code == 400 and "no longer available" in r.text
    conn = connect(app.state.settings.db_path)
    try:
        assert get_appointment(conn, tid, aid)["starts_at"] == "2031-01-01 10:00"   # unchanged
    finally:
        conn.close()


def test_reschedule_unavailable_when_no_open_hours(client, app):
    creds = onboard_studio(client, email="resc_none@example.com")
    login_owner(client, creds)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        a = _confirmed(conn, tid)
        conn.commit()
        tok = a["token"]
    finally:
        conn.close()
    # no availability windows → no reschedule link, and the page bounces back
    assert "Reschedule" not in client.get(f"/book/{tok}").text
    pub = CSRFClient(app)
    assert pub.get(f"/book/{tok}/reschedule", follow_redirects=False).status_code == 303

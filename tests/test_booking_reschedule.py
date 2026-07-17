"""Client self-reschedule — movement, notification replacement, and public flow."""

import json

from conftest import CSRFClient, login_owner, onboard_studio

from hestia.availability import available_slots
from hestia.crm import create_client
from hestia.db import connect, list_audit
from hestia.email import list_emails
from hestia.jobs import drain
from hestia.scheduler import (
    _notify,
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


def _notification_jobs(conn, appointment_id):
    jobs = []
    for row in conn.execute(
        "SELECT * FROM jobs WHERE kind = 'scheduler.notify' ORDER BY id"
    ).fetchall():
        payload = json.loads(row["payload_json"])
        if payload.get("appointment_id") == appointment_id:
            jobs.append({**dict(row), "payload": payload})
    return jobs


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


def test_same_time_module_retry_has_no_side_effects(conn, settings):
    t = _tenant(conn)
    a = _confirmed(conn, t["id"])
    conn.commit()
    before_jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id")]
    before_audit = list_audit(conn, t["id"])
    before_email = list_emails(conn, t["id"])

    assert reschedule_by_token(conn, settings, token=a["token"], new_slot=a["starts_at"]) is False
    assert [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id")] == before_jobs
    assert list_audit(conn, t["id"]) == before_audit
    assert list_emails(conn, t["id"]) == before_email


def test_reschedule_supersedes_pending_pair_and_stale_aba_job(conn, settings):
    t = _tenant(conn)
    client = create_client(conn, tenant_id=t["id"], name="Resa", email="resa@example.com")
    original = _confirmed(conn, t["id"], client_id=client["id"])
    conn.commit()
    old_jobs = _notification_jobs(conn, original["id"])
    assert {job["payload"]["kind"] for job in old_jobs} == {"confirm", "reminder"}
    assert len({job["payload"]["notification_generation"] for job in old_jobs}) == 1
    old_reminder = next(job for job in old_jobs if job["payload"]["kind"] == "reminder")
    legacy_id = conn.execute(
        "INSERT INTO jobs (tenant_id, kind, payload_json) VALUES (?, 'scheduler.notify', ?)",
        (t["id"], json.dumps({"appointment_id": original["id"], "kind": "reminder"})),
    ).lastrowid
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (old_reminder["id"],))
    conn.commit()

    moved = "2031-02-02 14:00"
    assert reschedule_by_token(conn, settings, token=original["token"], new_slot=moved) is True
    assert reschedule_by_token(
        conn, settings, token=original["token"], new_slot=original["starts_at"]
    ) is True
    conn.commit()

    jobs = _notification_jobs(conn, original["id"])
    assert next(job for job in jobs if job["id"] == old_reminder["id"])["status"] == "running"
    superseded = [job for job in jobs if job["status"] == "done"]
    assert superseded and all(job["payload"]["superseded"] for job in superseded)
    assert next(job for job in jobs if job["id"] == legacy_id)["status"] == "done"
    active = [job for job in jobs if job["status"] == "queued"]
    assert len(active) == 2
    assert {job["payload"]["kind"] for job in active} == {"confirm", "reminder"}
    assert {job["payload"]["expected_starts_at"] for job in active} == {original["starts_at"]}
    assert len({job["payload"]["notification_generation"] for job in active}) == 1

    # The old A-generation was already claimed, so superseding could not rewrite it.
    # Even after A→B→A makes its expected time look current again, generation binding
    # must keep it from sending.
    _notify(settings, old_reminder["payload"])
    assert list_emails(conn, t["id"]) == []

    newest_reminder = next(job for job in active if job["payload"]["kind"] == "reminder")
    conn.execute("UPDATE jobs SET run_at = datetime('now') WHERE id = ?", (newest_reminder["id"],))
    conn.commit()
    drain(settings.db_path, settings)
    messages = list_emails(conn, t["id"])
    assert len([msg for msg in messages if msg["subject"].startswith("Confirmed:")]) == 1
    assert len([msg for msg in messages if msg["subject"].startswith("Reminder:")]) == 1


def test_reschedule_superseding_is_tenant_scoped(conn, settings):
    first = _tenant(conn, "First")
    second = _tenant(conn, "Second")
    first_appt = _confirmed(conn, first["id"])
    same_tenant_appt = _confirmed(conn, first["id"], at="2031-03-03 15:00")
    second_appt = _confirmed(conn, second["id"])
    conn.commit()

    assert reschedule_by_token(
        conn, settings, token=first_appt["token"], new_slot="2031-02-02 14:00"
    ) is True
    assert {job["status"] for job in _notification_jobs(conn, same_tenant_appt["id"])} == {"queued"}
    assert {job["status"] for job in _notification_jobs(conn, second_appt["id"])} == {"queued"}


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


def test_http_same_time_retry_redirects_without_side_effects(client, app):
    creds = onboard_studio(client, email="resc_same@example.com")
    login_owner(client, creds)
    _open_all_week(client)
    conn = connect(app.state.settings.db_path)
    try:
        tid = _tid_of(conn, creds["email"])
        appointment = _confirmed(conn, tid)
        conn.commit()
        before_jobs = [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id")]
        before_audit = list_audit(conn, tid)
        before_email = list_emails(conn, tid)
    finally:
        conn.close()

    public = CSRFClient(app)
    response = public.post(
        f"/book/{appointment['token']}/reschedule",
        data={"slot": appointment["starts_at"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    conn = connect(app.state.settings.db_path)
    try:
        assert [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY id")] == before_jobs
        assert list_audit(conn, tid) == before_audit
        assert list_emails(conn, tid) == before_email
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

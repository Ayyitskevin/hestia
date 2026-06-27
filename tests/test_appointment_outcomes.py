"""Appointment outcomes — owner closes out a confirmed session as completed or a
no-show. Guarded (confirmed-only), idempotent, tenant-scoped; labels surface in the
schedule + detail views."""

from conftest import login_owner, onboard_studio

from hestia.scheduler import (
    complete_appointment,
    confirm_appointment,
    create_appointment,
    get_appointment,
    list_appointments,
    mark_no_show,
)
from hestia.tenants import create_tenant


def _confirmed(conn, tid, *, title="Shoot"):
    a = create_appointment(conn, tenant_id=tid, title=title, options=["2030-01-01 10:00"])
    confirm_appointment(conn, tid, a["id"], "2030-01-01 10:00")
    return a


# ── model ────────────────────────────────────────────────────────────────────


def test_complete_is_guarded_and_idempotent(conn):
    t = create_tenant(conn, name="S", shoot_type="wedding")
    a = _confirmed(conn, t["id"])
    assert complete_appointment(conn, t["id"], a["id"]) is True
    assert get_appointment(conn, t["id"], a["id"])["status"] == "completed"
    assert complete_appointment(conn, t["id"], a["id"]) is False     # no longer confirmed


def test_mark_no_show(conn):
    t = create_tenant(conn, name="S2", shoot_type="wedding")
    a = _confirmed(conn, t["id"])
    assert mark_no_show(conn, t["id"], a["id"]) is True
    assert get_appointment(conn, t["id"], a["id"])["status"] == "no_show"


def test_outcomes_require_confirmed(conn):
    t = create_tenant(conn, name="S3", shoot_type="wedding")
    a = create_appointment(conn, tenant_id=t["id"], title="Proposed", options=["2030-01-01 10:00"])
    assert complete_appointment(conn, t["id"], a["id"]) is False      # still proposed
    assert mark_no_show(conn, t["id"], a["id"]) is False


def test_outcomes_are_tenant_scoped(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    appt = _confirmed(conn, a["id"])
    assert complete_appointment(conn, b["id"], appt["id"]) is False   # B can't close A's
    assert mark_no_show(conn, b["id"], appt["id"]) is False
    assert get_appointment(conn, a["id"], appt["id"])["status"] == "confirmed"


def test_status_label_attached(conn):
    t = create_tenant(conn, name="L", shoot_type="wedding")
    a = _confirmed(conn, t["id"])
    mark_no_show(conn, t["id"], a["id"])
    assert get_appointment(conn, t["id"], a["id"])["status_label"] == "No-show"
    assert list_appointments(conn, t["id"])[0]["status_label"] == "No-show"


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_http_complete_and_no_show(client, conn):
    login_owner(client, onboard_studio(client, email="out@owner.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    done = _confirmed(conn, tid, title="Done shoot")
    missed = _confirmed(conn, tid, title="Missed shoot")
    conn.commit()

    client.post(f"/schedule/{done['id']}/complete")
    assert "Completed" in client.get(f"/schedule/{done['id']}").text
    assert conn.execute("SELECT status FROM appointments WHERE id=?",
                        (done["id"],)).fetchone()["status"] == "completed"

    client.post(f"/schedule/{missed['id']}/no-show")
    assert "No-show" in client.get(f"/schedule/{missed['id']}").text
    assert conn.execute("SELECT status FROM appointments WHERE id=?",
                        (missed["id"],)).fetchone()["status"] == "no_show"

    sched = client.get("/schedule")                                  # list renders both labels
    assert sched.status_code == 200 and "No-show" in sched.text and "Completed" in sched.text

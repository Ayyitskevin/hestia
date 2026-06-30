"""Scheduler — booking idempotency, confirm/reminder jobs, events, isolation."""

import json

from conftest import login_owner, onboard_studio

from hestia.automations import create_automation
from hestia.crm import create_client, create_project
from hestia.email import list_emails
from hestia.jobs import drain
from hestia.scheduler import (
    _notify,
    book_appointment,
    cancel_appointment,
    confirm_appointment,
    create_appointment,
    get_appointment,
    get_appointment_by_token,
    list_appointments,
)
from hestia.tenants import create_tenant

FUTURE = "2030-01-15 14:00"
PAST = "2020-01-01 10:00"


def _tenant(conn, name="Schedule Studio"):
    t = create_tenant(conn, name=name, shoot_type="wedding")
    conn.commit()
    return t


def _scheduler_jobs(conn):
    rows = conn.execute(
        "SELECT payload_json, run_at FROM jobs WHERE kind = 'scheduler.notify'"
    ).fetchall()
    return [{"kind": json.loads(r["payload_json"]).get("kind"), "run_at": r["run_at"]} for r in rows]


def test_create_with_options(conn):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah")
    p = create_project(conn, tenant_id=t["id"], name="Wedding", client_id=c["id"])
    appt = create_appointment(conn, tenant_id=t["id"], title="Consult", kind="consultation",
                              options=[FUTURE, "   ", "2030-01-16 10:00"],
                              client_id=c["id"], project_id=p["id"])
    assert appt["status"] == "proposed" and appt["token"]
    assert [o["starts_at"] for o in appt["options"]] == [FUTURE, "2030-01-16 10:00"]
    assert [o["sequence"] for o in appt["options"]] == [1, 2]
    assert appt["client_name"] == "Sarah" and appt["project_name"] == "Wedding"


def test_create_drops_foreign_parent_ids(conn):
    a = _tenant(conn, "A")
    b = _tenant(conn, "B")
    foreign_client = create_client(conn, tenant_id=a["id"], name="Foreign")
    foreign_project = create_project(conn, tenant_id=a["id"], name="Foreign Project")
    appt = create_appointment(
        conn, tenant_id=b["id"], title="Consult", options=[FUTURE],
        client_id=foreign_client["id"], project_id=foreign_project["id"],
    )
    assert appt["client_id"] is None and appt["project_id"] is None


def test_create_drops_project_for_wrong_same_tenant_client(conn):
    t = _tenant(conn)
    sarah = create_client(conn, tenant_id=t["id"], name="Sarah")
    bob = create_client(conn, tenant_id=t["id"], name="Bob")
    bob_project = create_project(conn, tenant_id=t["id"], name="Bob shoot", client_id=bob["id"])
    appt = create_appointment(
        conn, tenant_id=t["id"], title="Consult", options=[FUTURE],
        client_id=sarah["id"], project_id=bob_project["id"],
    )
    assert appt["client_id"] == sarah["id"]
    assert appt["project_id"] is None
    assert appt["client_name"] == "Sarah"
    assert appt["project_name"] is None


def test_reads_hide_malformed_same_tenant_project_link(conn):
    t = _tenant(conn)
    sarah = create_client(conn, tenant_id=t["id"], name="Sarah")
    bob = create_client(conn, tenant_id=t["id"], name="Bob")
    bob_project = create_project(conn, tenant_id=t["id"], name="Bob shoot", client_id=bob["id"])
    appt = create_appointment(
        conn, tenant_id=t["id"], title="Consult", options=[FUTURE], client_id=sarah["id"],
    )
    conn.execute(
        "UPDATE appointments SET project_id = ? WHERE id = ?",
        (bob_project["id"], appt["id"]),
    )
    got = get_appointment(conn, t["id"], appt["id"])
    public = get_appointment_by_token(conn, appt["token"])
    assert got["client_id"] == sarah["id"]
    assert got["project_id"] is None and got["project_name"] is None
    assert public["project_id"] is None and public["project_name"] is None
    assert list_appointments(conn, t["id"], project_id=bob_project["id"]) == []


def test_book_is_idempotent(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Consult",
                              options=[FUTURE, "2030-01-16 10:00"], client_id=c["id"])
    opt1 = appt["options"][0]["id"]
    assert book_appointment(conn, token=appt["token"], option_id=opt1) is True
    booked = get_appointment(conn, t["id"], appt["id"])
    assert booked["status"] == "confirmed" and booked["starts_at"] == FUTURE
    # a second booking is a no-op — the original time stands
    assert book_appointment(conn, token=appt["token"], option_id=appt["options"][1]["id"]) is False
    assert get_appointment(conn, t["id"], appt["id"])["starts_at"] == FUTURE


def test_book_rejects_foreign_option(conn):
    t = _tenant(conn)
    a = create_appointment(conn, tenant_id=t["id"], title="A", options=[FUTURE])
    b = create_appointment(conn, tenant_id=t["id"], title="B", options=["2030-02-02 09:00"])
    # booking A with B's option id must fail
    assert book_appointment(conn, token=a["token"], option_id=b["options"][0]["id"]) is False
    assert get_appointment(conn, t["id"], a["id"])["status"] == "proposed"


def test_owner_confirm_then_locked(conn):
    t = _tenant(conn)
    appt = create_appointment(conn, tenant_id=t["id"], title="Consult", options=[FUTURE])
    assert confirm_appointment(conn, t["id"], appt["id"], FUTURE) is True
    assert get_appointment(conn, t["id"], appt["id"])["status"] == "confirmed"
    # confirming again is a no-op (already confirmed, not proposed)
    assert confirm_appointment(conn, t["id"], appt["id"], "2030-03-03 12:00") is False


def test_confirm_enqueues_confirmation_and_reminder(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Consultation",
                              options=[FUTURE], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])
    conn.commit()

    jobs = _scheduler_jobs(conn)
    kinds = sorted(j["kind"] for j in jobs)
    assert kinds == ["confirm", "reminder"]
    # the reminder is scheduled for the future (a day before the session)
    reminder = next(j for j in jobs if j["kind"] == "reminder")
    now = conn.execute("SELECT datetime('now') AS n").fetchone()["n"]
    assert reminder["run_at"] > now

    drain(settings.db_path, settings)  # runs the immediate confirmation, not the future reminder
    sent = [m for m in list_emails(conn, t["id"]) if m["to_addr"] == "sarah@example.com"]
    assert any(m["subject"].startswith("Confirmed:") for m in sent)


def test_no_reminder_for_unparseable_time(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Consult",
                              options=["whenever works"], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])
    conn.commit()
    # only the confirmation job — no reminder for an unparseable time
    assert sorted(j["kind"] for j in _scheduler_jobs(conn)) == ["confirm"]


def test_no_reminder_for_past_time(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Consult",
                              options=[PAST], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])
    conn.commit()
    assert sorted(j["kind"] for j in _scheduler_jobs(conn)) == ["confirm"]


def test_reminder_handler_skips_canceled(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Consult",
                              options=[FUTURE], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])
    cancel_appointment(conn, t["id"], appt["id"])
    conn.commit()
    # firing the reminder for a canceled session sends nothing
    _notify(settings, {"appointment_id": appt["id"], "kind": "reminder"})
    assert all(not m["subject"].startswith("Reminder:") for m in list_emails(conn, t["id"]))


def test_reminder_handler_sends_when_confirmed(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    appt = create_appointment(conn, tenant_id=t["id"], title="Shoot",
                              options=[FUTURE], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])
    conn.commit()
    _notify(settings, {"appointment_id": appt["id"], "kind": "reminder"})
    assert any(m["subject"].startswith("Reminder:") for m in list_emails(conn, t["id"]))


def test_confirmed_fires_automation(conn, settings):
    t = _tenant(conn)
    c = create_client(conn, tenant_id=t["id"], name="Sarah", email="sarah@example.com")
    create_automation(conn, tenant_id=t["id"], name="welcome", trigger="appointment.confirmed",
                      subject="See you soon, {client_name}", body="Looking forward to {title}.")
    appt = create_appointment(conn, tenant_id=t["id"], title="Consultation",
                              options=[FUTURE], client_id=c["id"])
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])
    conn.commit()
    drain(settings.db_path, settings)
    assert any(m["subject"] == "See you soon, Sarah" for m in list_emails(conn, t["id"]))


def test_tenant_isolation(conn):
    t1, t2 = _tenant(conn, "A"), _tenant(conn, "B")
    create_appointment(conn, tenant_id=t1["id"], title="A-appt", options=[FUTURE])
    assert list_appointments(conn, t2["id"]) == []
    assert get_appointment_by_token(conn, "nope") is None


def test_http_book_flow(client):
    creds = onboard_studio(client, email="sched@example.com")
    login_owner(client, creds)
    r = client.post("/schedule", data={
        "title": "Engagement consult", "kind": "consultation",
        "options": f"{FUTURE}\n2030-01-16 10:00", "duration_minutes": "45",
    })
    aid = r.url.path.rstrip("/").split("/")[-1]
    detail = client.get(f"/schedule/{aid}")
    assert detail.status_code == 200 and "/book/" in detail.text
    token = detail.text.split("/book/")[1].split('"')[0].split("<")[0].strip()

    page = client.get(f"/book/{token}")
    assert page.status_code == 200 and FUTURE in page.text
    # the radio options are named option_id
    oid = page.text.split('name="option_id" value="')[1].split('"')[0]
    client.post(f"/book/{token}", data={"option_id": oid})
    confirmed = client.get(f"/book/{token}")
    assert "booked" in confirmed.text.lower() and FUTURE in confirmed.text
    # owner detail now shows confirmed
    assert "Confirmed" in client.get(f"/schedule/{aid}").text


def test_http_unknown_and_canceled_book_404(client):
    creds = onboard_studio(client, email="x@example.com")
    login_owner(client, creds)
    assert client.get("/book/nope-not-a-token").status_code == 404
    r = client.post("/schedule", data={"title": "S", "options": FUTURE})
    aid = r.url.path.rstrip("/").split("/")[-1]
    token = client.get(f"/schedule/{aid}").text.split("/book/")[1].split('"')[0].split("<")[0].strip()
    assert client.get(f"/book/{token}").status_code == 200
    client.post(f"/schedule/{aid}/cancel")
    assert client.get(f"/book/{token}").status_code == 404

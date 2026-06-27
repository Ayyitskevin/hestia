"""Client-side cancellation — a client cancels their own booking from the link:
proposed/confirmed → canceled (once), the studio owner is alerted, and the now-dead
link still 404s on a direct revisit (unchanged contract)."""

from conftest import login_owner, onboard_studio

from hestia.crm import create_client
from hestia.email import list_emails
from hestia.scheduler import (
    book_appointment,
    cancel_by_token,
    create_appointment,
    get_appointment,
)
from hestia.tenants import create_tenant, create_user


def _booked(conn, tid, *, title="Engagement", client_id=None):
    appt = create_appointment(conn, tenant_id=tid, title=title, options=["2030-01-01 10:00"],
                              client_id=client_id)
    book_appointment(conn, token=appt["token"], option_id=appt["options"][0]["id"])  # → confirmed
    return appt


# ── model ────────────────────────────────────────────────────────────────────


def test_cancel_by_token_cancels_and_alerts_owner(conn, settings):
    t = create_tenant(conn, name="Sch", shoot_type="wedding")
    create_user(conn, tenant_id=t["id"], email="owner@sch.com", password="pw12345678", role="owner")
    c = create_client(conn, tenant_id=t["id"], name="Sam", email="sam@ex.com")
    appt = _booked(conn, t["id"], client_id=c["id"])
    conn.commit()

    assert cancel_by_token(conn, settings, appt["token"]) is True
    assert get_appointment(conn, t["id"], appt["id"])["status"] == "canceled"
    alerts = [m for m in list_emails(conn, t["id"])
              if m["to_addr"] == "owner@sch.com" and m["subject"].startswith("Canceled:")]
    assert len(alerts) == 1 and "Sam" in alerts[0]["body"]


def test_cancel_by_token_is_idempotent(conn, settings):
    t = create_tenant(conn, name="Sch2", shoot_type="wedding")
    create_user(conn, tenant_id=t["id"], email="owner@sch2.com", password="pw12345678", role="owner")
    appt = _booked(conn, t["id"])
    conn.commit()
    assert cancel_by_token(conn, settings, appt["token"]) is True
    assert cancel_by_token(conn, settings, appt["token"]) is False        # already canceled
    alerts = [m for m in list_emails(conn, t["id"]) if m["subject"].startswith("Canceled:")]
    assert len(alerts) == 1                                               # no second alert


def test_cancel_by_token_works_on_proposed(conn, settings):
    t = create_tenant(conn, name="Sch3", shoot_type="wedding")
    appt = create_appointment(conn, tenant_id=t["id"], title="Maybe", options=["2030-01-01 10:00"])
    conn.commit()                                                        # still proposed (unbooked)
    assert cancel_by_token(conn, settings, appt["token"]) is True
    assert get_appointment(conn, t["id"], appt["id"])["status"] == "canceled"


def test_cancel_unknown_token_is_false(conn, settings):
    assert cancel_by_token(conn, settings, "no-such-token") is False


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_http_client_cancel_flow(client, conn):
    login_owner(client, onboard_studio(client, name="Cx Studio", email="owner@cx.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    appt = _booked(conn, tid)
    conn.commit()
    token = appt["token"]

    r = client.post(f"/book/{token}/cancel")
    assert r.status_code == 200 and "canceled" in r.text.lower()
    assert conn.execute("SELECT status FROM appointments WHERE id=?",
                        (appt["id"],)).fetchone()["status"] == "canceled"
    assert client.get(f"/book/{token}").status_code == 404               # dead link still 404s
    assert any(m["subject"].startswith("Canceled:") for m in list_emails(conn, tid))


def test_http_cancel_unknown_and_double_cancel_404(client, conn):
    login_owner(client, onboard_studio(client, name="Dx Studio", email="owner@dx.com"))
    tid = conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]
    appt = _booked(conn, tid)
    conn.commit()
    assert client.post("/book/not-a-token/cancel").status_code == 404
    assert client.post(f"/book/{appt['token']}/cancel").status_code == 200   # first cancel ok
    assert client.post(f"/book/{appt['token']}/cancel").status_code == 404   # already canceled

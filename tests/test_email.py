"""Email seam — pluggable mock|smtp emailer, outbox recording, and wiring."""

import dataclasses

from conftest import login_owner, onboard_studio
from fastapi.testclient import TestClient

from hestia.email import MockEmailer, SmtpEmailer, build_emailer, list_emails, notify
from hestia.tenants import create_tenant

# ── seam primitives ─────────────────────────────────────────────────────────


def test_build_emailer_selection(settings):
    assert isinstance(build_emailer(settings), MockEmailer)
    assert isinstance(build_emailer(dataclasses.replace(settings, email_backend="smtp")),
                      SmtpEmailer)


def test_mock_records_to_outbox(conn, settings):
    t = create_tenant(conn, name="Outbox Co", shoot_type="other")
    status = notify(conn, settings, to="a@b.com", subject="Hi", body="Body", tenant_id=t["id"])
    conn.commit()
    assert status == "recorded"
    rows = list_emails(conn, t["id"])
    assert len(rows) == 1
    assert rows[0]["to_addr"] == "a@b.com" and rows[0]["backend"] == "mock"


def test_notify_is_noop_without_recipient(conn, settings):
    t = create_tenant(conn, name="No Recipient", shoot_type="other")
    assert notify(conn, settings, to="", subject="x", body="y", tenant_id=t["id"]) is None
    assert notify(conn, settings, to="   ", subject="x", body="y", tenant_id=t["id"]) is None
    assert list_emails(conn, t["id"]) == []


def test_smtp_captures_error_without_host(conn, settings):
    # smtp backend with no host must degrade: record an error status, never raise.
    s = dataclasses.replace(settings, email_backend="smtp", smtp_host="")
    t = create_tenant(conn, name="Smtp Co", shoot_type="other")
    status = notify(conn, s, to="a@b.com", subject="x", body="y", tenant_id=t["id"])
    conn.commit()
    assert status.startswith("error")
    assert list_emails(conn, t["id"])[0]["status"].startswith("error")


# ── wiring: invoice send → email the client the pay link ────────────────────


def _make_client_with_email(client, conn, email):
    client.post("/clients", data={"name": "Pat", "email": email})
    return conn.execute("SELECT id FROM clients WHERE email = ?", (email,)).fetchone()["id"]


def test_invoice_send_emails_client_pay_link(client, conn):
    login_owner(client, onboard_studio(client, email="owner@inv.com"))
    cid = _make_client_with_email(client, conn, "pat@example.com")
    r = client.post("/invoices", data={"title": "Balance", "amount": "500",
                                       "client_id": str(cid)})
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/send")

    rows = conn.execute("SELECT * FROM emails WHERE to_addr = 'pat@example.com'").fetchall()
    assert len(rows) == 1
    assert "/pay/" in rows[0]["body"] and rows[0]["status"] == "recorded"


def test_invoice_send_without_client_email_sends_nothing(client, conn):
    login_owner(client, onboard_studio(client, email="owner2@inv.com"))
    r = client.post("/invoices", data={"title": "Walk-in", "amount": "100"})  # no client
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/send")
    assert conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"] == 0


# ── wiring: public inquiry → alert the studio ───────────────────────────────


def test_inquiry_alerts_the_studio(client, conn):
    login_owner(client, onboard_studio(client, name="Lead Studio", email="owner3@inq.com"))
    client.post("/settings/site", data={"headline": "Hi", "published": "1"})
    slug = conn.execute("SELECT slug FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["slug"]

    TestClient(client.app).post(  # anonymous visitor — no session, no CSRF needed
        f"/studio/{slug}/inquire",
        data={"name": "Sky", "email": "sky@lead.com", "shoot_type": "wedding"})

    row = conn.execute(
        "SELECT * FROM emails WHERE subject LIKE 'New wedding inquiry%'").fetchone()
    assert row is not None
    assert row["to_addr"] == "owner3@inq.com"  # no contact_email set → owner login
    assert "sky@lead.com" in row["body"]


# ── outbox view ─────────────────────────────────────────────────────────────


def test_outbox_view_lists_recorded_emails(client, conn):
    login_owner(client, onboard_studio(client, email="owner4@out.com"))
    cid = _make_client_with_email(client, conn, "see@me.com")
    r = client.post("/invoices", data={"title": "Shoot", "amount": "750", "client_id": str(cid)})
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/send")

    page = client.get("/settings/outbox")
    assert page.status_code == 200
    assert "Email outbox" in page.text
    assert "see@me.com" in page.text

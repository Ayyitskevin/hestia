"""Studio email signature — appended to client-facing mail, kept off account mail,
saved from settings, and strictly tenant-scoped."""

from conftest import login_owner, onboard_studio
from fastapi.testclient import TestClient

from hestia.email import list_emails, notify
from hestia.tenants import create_tenant, set_email_signature

SIG = "Warm regards,\nJane's Studio\n(555) 123-4567"


# ── chokepoint: notify() signs client mail, skips account mail ────────────────


def test_signature_appended_to_client_mail(conn, settings):
    t = create_tenant(conn, name="Signed Co", shoot_type="other")
    set_email_signature(conn, t["id"], SIG)
    notify(conn, settings, to="c@b.com", subject="Hi", body="Your gallery is ready.",
           tenant_id=t["id"])
    conn.commit()
    body = list_emails(conn, t["id"])[0]["body"]
    assert body == "Your gallery is ready.\n\n—\nWarm regards,\nJane's Studio\n(555) 123-4567"


def test_account_mail_stays_unsigned(conn, settings):
    """signed=False (verify, reset, lead alerts) never carries the signature."""
    t = create_tenant(conn, name="Plain Co", shoot_type="other")
    set_email_signature(conn, t["id"], SIG)
    notify(conn, settings, to="o@b.com", subject="Reset", body="Reset link: /x",
           tenant_id=t["id"], signed=False)
    conn.commit()
    body = list_emails(conn, t["id"])[0]["body"]
    assert body == "Reset link: /x" and "Jane's Studio" not in body


def test_no_signature_leaves_body_untouched(conn, settings):
    t = create_tenant(conn, name="Blank Co", shoot_type="other")  # default '' signature
    notify(conn, settings, to="c@b.com", subject="Hi", body="Plain body.", tenant_id=t["id"])
    conn.commit()
    body = list_emails(conn, t["id"])[0]["body"]
    assert body == "Plain body." and "—" not in body


def test_signature_is_tenant_scoped(conn, settings):
    """One studio's signature never leaks onto another studio's mail."""
    a = create_tenant(conn, name="Studio A", shoot_type="other")
    b = create_tenant(conn, name="Studio B", shoot_type="other")
    set_email_signature(conn, a["id"], SIG)               # only A has a signature
    notify(conn, settings, to="c@b.com", subject="Hi", body="Bs message.", tenant_id=b["id"])
    conn.commit()
    assert list_emails(conn, b["id"])[0]["body"] == "Bs message."


def test_set_email_signature_trims_and_caps(conn):
    t = create_tenant(conn, name="Cap Co", shoot_type="other")
    set_email_signature(conn, t["id"], "  " + "x" * 900 + "  ")
    conn.commit()
    stored = conn.execute(
        "SELECT email_signature FROM tenants WHERE id = ?", (t["id"],)).fetchone()[0]
    assert stored == "x" * 600                            # stripped, capped at 600


# ── settings round-trip + real client-facing wiring (invoice send) ───────────


def test_signature_settings_roundtrip_and_signs_invoice(client, conn):
    login_owner(client, onboard_studio(client, email="owner@sig.com"))
    client.post("/settings/signature", data={"email_signature": SIG})

    page = client.get("/settings/site")                   # textarea pre-filled with the saved sig
    assert page.status_code == 200 and "(555) 123-4567" in page.text  # apostrophe is HTML-escaped

    client.post("/clients", data={"name": "Pat", "email": "pat@sig.com"})
    cid = conn.execute("SELECT id FROM clients WHERE email = 'pat@sig.com'").fetchone()["id"]
    r = client.post("/invoices", data={"title": "Balance", "amount": "500", "client_id": str(cid)})
    iid = int(str(r.url).rstrip("/").split("/")[-1])
    client.post(f"/invoices/{iid}/send")

    body = conn.execute("SELECT body FROM emails WHERE to_addr = 'pat@sig.com'").fetchone()["body"]
    assert "/pay/" in body and body.rstrip().endswith("Jane's Studio\n(555) 123-4567")


def test_inquiry_alert_to_owner_stays_unsigned(client, conn):
    """The lead alert is owner-facing — it must not carry the studio's client sign-off."""
    login_owner(client, onboard_studio(client, name="Lead Studio", email="owner@lead.com"))
    client.post("/settings/signature", data={"email_signature": SIG})
    client.post("/settings/site", data={"headline": "Hi", "published": "1"})
    slug = conn.execute("SELECT slug FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["slug"]

    TestClient(client.app).post(  # anonymous visitor — no session, no CSRF needed
        f"/studio/{slug}/inquire",
        data={"name": "Sky", "email": "sky@lead.com", "shoot_type": "wedding"})

    row = conn.execute("SELECT body FROM emails WHERE subject LIKE 'New wedding inquiry%'").fetchone()
    assert row is not None and "Jane's Studio" not in row["body"]

"""Public beta interest capture before full self-serve signup."""

import dataclasses

from conftest import CSRFClient

from hestia.interest import record_beta_interest, send_beta_interest_invite
from hestia.main import create_app


def _client(settings, **overrides):
    return CSRFClient(create_app(dataclasses.replace(settings, **overrides)))


def _latest_invite_token(conn, email: str) -> str:
    body = conn.execute(
        "SELECT body FROM emails WHERE to_addr = ? AND subject LIKE '%invited%' "
        "ORDER BY id DESC LIMIT 1",
        (email,),
    ).fetchone()["body"]
    return body.split("/invite/", 1)[1].split()[0].strip()


def test_interest_form_preserves_public_attribution(settings):
    page = _client(settings).get("/interest?source=pricing&path=/pricing")

    assert page.status_code == 200
    assert "Request beta access" in page.text
    assert 'name="signup_source" value="pricing"' in page.text
    assert 'name="signup_landing_path" value="/pricing"' in page.text


def test_interest_records_lead_and_notifies_operator(settings, conn):
    client = _client(settings, smtp_from="founder@hestia.test")
    response = client.post("/interest", data={
        "name": "Pat Photographer",
        "studio_name": "Pat Studio",
        "email": "PAT@EXAMPLE.COM",
        "shoot_type": "food",
        "note": "Need galleries, booking, and invoices.",
        "signup_source": "demo",
        "signup_landing_path": "/demo/food",
    })

    assert response.status_code == 200
    assert "beta list" in response.text
    lead = conn.execute("SELECT * FROM beta_interests WHERE email = ?",
                        ("pat@example.com",)).fetchone()
    assert lead["studio_name"] == "Pat Studio"
    assert lead["shoot_type"] == "food"
    assert lead["source"] == "demo"
    assert lead["landing_path"] == "/demo/food"
    mail = conn.execute(
        "SELECT * FROM emails WHERE to_addr = ? ORDER BY id DESC LIMIT 1",
        ("founder@hestia.test",),
    ).fetchone()
    assert mail["subject"] == "New Hestia beta interest: Pat Studio"
    assert "Need galleries, booking, and invoices." in mail["body"]


def test_interest_duplicate_email_updates_existing_lead(settings, conn):
    client = _client(settings)
    client.post("/interest", data={
        "name": "First",
        "studio_name": "First Studio",
        "email": "lead@example.com",
        "shoot_type": "wedding",
        "signup_source": "pricing",
        "signup_landing_path": "/pricing",
    })
    client.post("/interest", data={
        "name": "Second",
        "studio_name": "Second Studio",
        "email": "lead@example.com",
        "shoot_type": "real-estate",
        "note": "Listings and invoices.",
        "signup_source": "https://evil.example",
        "signup_landing_path": "//evil.example",
    })

    count = conn.execute("SELECT COUNT(*) AS n FROM beta_interests").fetchone()["n"]
    lead = conn.execute("SELECT * FROM beta_interests WHERE email = ?",
                        ("lead@example.com",)).fetchone()
    assert count == 1
    assert lead["name"] == "Second"
    assert lead["studio_name"] == "Second Studio"
    assert lead["shoot_type"] == "other"
    assert lead["source"] == ""
    assert lead["landing_path"] == ""
    assert lead["note"] == "Listings and invoices."


def test_interest_rejects_invalid_email(settings, conn):
    page = _client(settings).post("/interest", data={
        "email": "not-an-email",
        "studio_name": "Bad Lead",
    })

    assert page.status_code == 200
    assert "Enter a valid email address." in page.text
    assert conn.execute("SELECT COUNT(*) AS n FROM beta_interests").fetchone()["n"] == 0


def test_beta_interest_invite_records_status_and_private_link(settings, conn):
    interest = record_beta_interest(
        conn,
        settings,
        name="Invited Owner",
        studio_name="Invited Studio",
        email="invite@example.com",
        shoot_type="wedding",
        source="pricing",
        landing_path="/pricing",
    )

    result = send_beta_interest_invite(conn, settings, interest["id"])
    conn.commit()

    lead = conn.execute("SELECT * FROM beta_interests WHERE email = ?",
                        ("invite@example.com",)).fetchone()
    assert result["invite_url"].startswith(settings.public_url.rstrip("/") + "/invite/")
    assert lead["status"] == "invited"
    assert lead["invite_token_hash"]
    assert lead["invited_at"]
    assert lead["invite_expires_at"]
    assert lead["invite_email_status"] == "recorded"
    mail = conn.execute(
        "SELECT * FROM emails WHERE to_addr = ? ORDER BY id DESC LIMIT 1",
        ("invite@example.com",),
    ).fetchone()
    assert mail["subject"] == "You're invited to start your Hestia studio beta"
    assert "/invite/" in mail["body"]
    assert "exactly $40/month" in mail["body"]


def test_private_invite_signup_works_when_public_signup_is_disabled(settings, conn):
    interest = record_beta_interest(
        conn,
        settings,
        name="Private Owner",
        studio_name="Private Studio",
        email="private@example.com",
        shoot_type="food",
        source="demo",
        landing_path="/demo/food",
    )
    send_beta_interest_invite(conn, settings, interest["id"])
    conn.commit()
    token = _latest_invite_token(conn, "private@example.com")

    client = _client(settings, signup_enabled=False)
    form = client.get(f"/invite/{token}")
    assert form.status_code == 200
    assert "Private Studio" in form.text
    assert "private@example.com" in form.text

    response = client.post(f"/invite/{token}", data={
        "studio_name": "Private Studio Beta",
        "shoot_type": "food",
        "password": "password123",
    })

    assert response.status_code == 200
    assert "check your email" in response.text.lower()
    owner = conn.execute(
        """
        SELECT u.email, u.verified, t.id AS tenant_id, t.name, t.signup_source,
               t.signup_landing_path
          FROM users u
          JOIN tenants t ON t.id = u.tenant_id
         WHERE u.email = ?
        """,
        ("private@example.com",),
    ).fetchone()
    assert owner["verified"] == 0
    assert owner["name"] == "Private Studio Beta"
    assert owner["signup_source"] == "interest"
    assert owner["signup_landing_path"] == "/interest"
    lead = conn.execute("SELECT * FROM beta_interests WHERE email = ?",
                        ("private@example.com",)).fetchone()
    assert lead["status"] == "converted"
    assert lead["tenant_id"] == owner["tenant_id"]
    assert lead["converted_at"]
    assert lead["invite_token_hash"] == ""
    verify = conn.execute(
        "SELECT body FROM emails WHERE to_addr = ? AND subject LIKE 'Verify%'",
        ("private@example.com",),
    ).fetchone()
    assert "/verify/" in verify["body"]
    assert "invalid, expired, or already used" in client.get(f"/invite/{token}").text

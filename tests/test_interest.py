"""Public beta interest capture before full self-serve signup."""

import dataclasses

from conftest import CSRFClient

from hestia.main import create_app


def _client(settings, **overrides):
    return CSRFClient(create_app(dataclasses.replace(settings, **overrides)))


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

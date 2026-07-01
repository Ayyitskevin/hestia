"""Shareable public beta landing page."""

import dataclasses
from html import unescape

from conftest import CSRFClient

from hestia.main import create_app


def _client(settings, **overrides):
    return CSRFClient(create_app(dataclasses.replace(settings, **overrides)))


def test_beta_page_renders_value_copy_and_preserves_attribution(settings):
    page = _client(settings).get("/beta?source=pricing&path=/pricing")
    text = unescape(page.text)

    assert page.status_code == 200
    assert "Run the whole photography studio for $40/month." in text
    assert "14-day free trial" in text
    assert "Wedding:" in text
    assert "Food & beverage:" in text
    assert "Real estate:" in text
    assert 'name="signup_source" value="pricing"' in page.text
    assert 'name="signup_landing_path" value="/pricing"' in page.text


def test_beta_page_defaults_to_beta_attribution(settings):
    page = _client(settings).get("/beta")

    assert page.status_code == 200
    assert 'name="signup_source" value="beta"' in page.text
    assert 'name="signup_landing_path" value="/beta"' in page.text


def test_beta_page_records_interest_and_notifies_operator(settings, conn):
    client = _client(settings, smtp_from="founder@hestia.test")
    response = client.post("/beta", data={
        "name": "Bea Photographer",
        "studio_name": "Bea Studio",
        "email": "BEA@EXAMPLE.COM",
        "shoot_type": "wedding",
        "note": "Need one place for booking, galleries, and invoices.",
        "signup_source": "beta",
        "signup_landing_path": "/beta",
    })

    assert response.status_code == 200
    assert "You're on the Hestia beta list." in response.text
    lead = conn.execute("SELECT * FROM beta_interests WHERE email = ?",
                        ("bea@example.com",)).fetchone()
    assert lead["studio_name"] == "Bea Studio"
    assert lead["shoot_type"] == "wedding"
    assert lead["source"] == "beta"
    assert lead["landing_path"] == "/beta"
    mail = conn.execute(
        "SELECT * FROM emails WHERE to_addr = ? ORDER BY id DESC LIMIT 1",
        ("founder@hestia.test",),
    ).fetchone()
    assert mail["subject"] == "New Hestia beta interest: Bea Studio"
    assert "Source: Beta page /beta" in mail["body"]


def test_beta_page_rejects_invalid_email(settings, conn):
    page = _client(settings).post("/beta", data={
        "email": "not-an-email",
        "studio_name": "Bad Beta Lead",
    })

    assert page.status_code == 200
    assert "Enter a valid email address." in page.text
    assert "Run the whole photography studio for $40/month." in page.text
    assert conn.execute("SELECT COUNT(*) AS n FROM beta_interests").fetchone()["n"] == 0

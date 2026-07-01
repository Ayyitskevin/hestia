"""Public signup + email verification (gated by HESTIA_SIGNUP_ENABLED)."""

import dataclasses

from conftest import CSRFClient

from hestia.interest import record_beta_interest
from hestia.main import create_app
from hestia.presets import apply_preset


def _client(settings, *, enabled=True):
    return CSRFClient(create_app(dataclasses.replace(settings, signup_enabled=enabled)))


def _verify_token(conn, email):
    body = conn.execute("SELECT body FROM emails WHERE to_addr = ?", (email,)).fetchone()["body"]
    return body.split("/verify/")[1].split("\n")[0].strip()


# ── the gate ────────────────────────────────────────────────────────────────


def test_signup_disabled_redirects_to_login(settings):
    c = _client(settings, enabled=False)
    r = c.get("/signup", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    # POST is gated too
    r2 = c.post("/signup", data={"name": "X", "email": "x@y.com", "password": "password123"},
                follow_redirects=False)
    assert r2.status_code == 303


def test_signup_form_renders_when_enabled(settings):
    r = _client(settings).get("/signup")
    assert r.status_code == 200
    assert "Start your hosted studio" in r.text
    assert "14-day trial, then" in r.text and "$40/month" in r.text
    assert "No tiers" in r.text


def test_signup_form_preserves_public_attribution(settings):
    r = _client(settings).get("/signup?source=pricing&path=/pricing")

    assert r.status_code == 200
    assert 'name="signup_source" value="pricing"' in r.text
    assert 'name="signup_landing_path" value="/pricing"' in r.text


# ── happy path ──────────────────────────────────────────────────────────────


def test_signup_creates_unverified_owner_and_emails_link(settings, conn):
    c = _client(settings)
    r = c.post("/signup", data={"name": "New Studio", "email": "new@studio.com",
                                "password": "password123", "shoot_type": "wedding"})
    assert r.status_code == 200 and "check your email" in r.text.lower()

    user = conn.execute("SELECT * FROM users WHERE email = 'new@studio.com'").fetchone()
    assert user is not None and user["verified"] == 0
    mail = conn.execute("SELECT body FROM emails WHERE to_addr = 'new@studio.com'").fetchone()
    assert mail is not None and "/verify/" in mail["body"]


def test_signup_records_sanitized_first_party_attribution(settings, conn):
    c = _client(settings)
    r = c.post("/signup", data={
        "name": "Attributed Studio",
        "email": "attributed@studio.com",
        "password": "password123",
        "shoot_type": "wedding",
        "signup_source": "PRICING!!",
        "signup_landing_path": "/pricing?ignored=1",
    })

    assert r.status_code == 200
    tenant = conn.execute(
        "SELECT signup_source, signup_landing_path FROM tenants WHERE name = ?",
        ("Attributed Studio",),
    ).fetchone()
    assert tenant["signup_source"] == "pricing"
    assert tenant["signup_landing_path"] == "/pricing"


def test_signup_marks_matching_beta_interest_converted(settings, conn):
    record_beta_interest(
        conn,
        settings,
        name="Interest Owner",
        studio_name="Interest Studio",
        email="interest-signup@example.com",
        shoot_type="wedding",
        source="pricing",
        landing_path="/pricing",
    )
    conn.commit()

    c = _client(settings)
    r = c.post("/signup", data={
        "name": "Interest Studio",
        "email": "interest-signup@example.com",
        "password": "password123",
        "shoot_type": "wedding",
        "signup_source": "pricing",
        "signup_landing_path": "/pricing",
    })

    assert r.status_code == 200
    owner = conn.execute(
        "SELECT tenant_id FROM users WHERE email = ?",
        ("interest-signup@example.com",),
    ).fetchone()
    lead = conn.execute("SELECT * FROM beta_interests WHERE email = ?",
                        ("interest-signup@example.com",)).fetchone()
    assert lead["status"] == "converted"
    assert lead["tenant_id"] == owner["tenant_id"]
    assert lead["converted_at"]


def test_signup_rejects_external_attribution_path(settings, conn):
    c = _client(settings)
    c.post("/signup", data={
        "name": "Unknown Source",
        "email": "unknownsource@studio.com",
        "password": "password123",
        "signup_source": "https://evil.example",
        "signup_landing_path": "//evil.example/pricing",
    })

    tenant = conn.execute(
        "SELECT signup_source, signup_landing_path FROM tenants WHERE name = ?",
        ("Unknown Source",),
    ).fetchone()
    assert tenant["signup_source"] == ""
    assert tenant["signup_landing_path"] == ""


def test_unverified_owner_cannot_log_in(settings):
    c = _client(settings)
    c.post("/signup", data={"name": "S", "email": "unv@s.com", "password": "password123"})
    r = c.post("/login", data={"email": "unv@s.com", "password": "password123"})
    assert "verify your email" in r.text.lower()
    assert "/dashboard" not in str(r.url)


def test_verify_activates_and_starts_onboarding_session(settings, conn):
    c = _client(settings)
    c.post("/signup", data={"name": "Act Studio", "email": "act@s.com", "password": "password123"})
    token = _verify_token(conn, "act@s.com")

    done = c.get(f"/verify/{token}", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/onboarding"
    assert conn.execute("SELECT verified FROM users WHERE email='act@s.com'").fetchone()["verified"] == 1
    assert "Set up your studio command center" in c.get("/onboarding").text


def test_login_routes_fresh_studio_to_onboarding(settings, conn):
    c = _client(settings)
    c.post("/signup", data={"name": "Fresh Studio", "email": "fresh@s.com",
                            "password": "password123"})
    token = _verify_token(conn, "fresh@s.com")
    c.get(f"/verify/{token}")
    c.get("/logout")

    login = c.post("/login", data={"email": "fresh@s.com", "password": "password123"},
                   follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"] == "/onboarding"


def test_login_routes_configured_studio_to_dashboard(settings, conn):
    c = _client(settings)
    c.post("/signup", data={"name": "Ready Studio", "email": "ready@s.com",
                            "password": "password123"})
    token = _verify_token(conn, "ready@s.com")
    c.get(f"/verify/{token}")
    tenant_id = conn.execute(
        "SELECT tenant_id FROM users WHERE email='ready@s.com'",
    ).fetchone()["tenant_id"]
    apply_preset(conn, tenant_id, "wedding", include_demo=False)
    c.get("/logout")

    login = c.post("/login", data={"email": "ready@s.com", "password": "password123"},
                   follow_redirects=False)
    assert login.status_code == 303 and login.headers["location"] == "/dashboard"


# ── guards ──────────────────────────────────────────────────────────────────


def test_duplicate_email_is_rejected(settings):
    c = _client(settings)
    c.post("/signup", data={"name": "S1", "email": "dup@s.com", "password": "password123"})
    r = c.post("/signup", data={"name": "S2", "email": "dup@s.com", "password": "password123"})
    assert "already registered" in r.text


def test_short_password_is_rejected(settings, conn):
    c = _client(settings)
    r = c.post("/signup", data={"name": "S", "email": "short@s.com", "password": "short"})
    assert "at least 8" in r.text
    assert conn.execute("SELECT COUNT(*) AS n FROM users WHERE email='short@s.com'").fetchone()["n"] == 0


def test_bad_verify_token_shows_failure(settings):
    r = _client(settings).get("/verify/not-a-real-token")
    assert r.status_code == 200 and "invalid or has" in r.text.lower()


def test_verify_token_is_single_use(settings, conn):
    c = _client(settings)
    c.post("/signup", data={"name": "Once", "email": "once@s.com", "password": "password123"})
    token = _verify_token(conn, "once@s.com")
    assert c.get(f"/verify/{token}", follow_redirects=False).status_code == 303  # first use works
    assert "invalid or has" in c.get(f"/verify/{token}").text.lower()  # second use fails

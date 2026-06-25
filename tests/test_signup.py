"""Public signup + email verification (gated by HESTIA_SIGNUP_ENABLED)."""

import dataclasses

from conftest import CSRFClient

from hestia.main import create_app


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
    assert r.status_code == 200 and "Create your studio" in r.text


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


def test_unverified_owner_cannot_log_in(settings):
    c = _client(settings)
    c.post("/signup", data={"name": "S", "email": "unv@s.com", "password": "password123"})
    r = c.post("/login", data={"email": "unv@s.com", "password": "password123"})
    assert "verify your email" in r.text.lower()
    assert "/dashboard" not in str(r.url)


def test_verify_activates_then_login_succeeds(settings, conn):
    c = _client(settings)
    c.post("/signup", data={"name": "Act Studio", "email": "act@s.com", "password": "password123"})
    token = _verify_token(conn, "act@s.com")

    done = c.get(f"/verify/{token}")
    assert done.status_code == 200 and "Email verified" in done.text
    assert conn.execute("SELECT verified FROM users WHERE email='act@s.com'").fetchone()["verified"] == 1

    login = c.post("/login", data={"email": "act@s.com", "password": "password123"})
    assert "/dashboard" in str(login.url)


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
    assert "Email verified" in c.get(f"/verify/{token}").text          # first use works
    assert "invalid or has" in c.get(f"/verify/{token}").text.lower()  # second use fails

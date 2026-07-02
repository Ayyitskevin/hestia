"""Password reset — token lifecycle + the forgot/reset HTTP flow."""

from datetime import timedelta

from conftest import login_owner, onboard_studio

from hestia.resets import consume_reset, create_reset, find_reset
from hestia.tenants import create_tenant, create_user


def _user(conn, email="u@r.com"):
    t = create_tenant(conn, name="Reset Co", shoot_type="other")
    return create_user(conn, tenant_id=t["id"], email=email, password="oldpassword")


# ── token lifecycle ─────────────────────────────────────────────────────────


def test_create_find_consume(conn, settings):
    u = _user(conn)
    token = create_reset(conn, settings, user_id=u["id"])
    conn.commit()
    assert find_reset(conn, settings, token)["user_id"] == u["id"]
    assert find_reset(conn, settings, "not-a-real-token") is None
    assert consume_reset(conn, settings, token) == u["id"]
    conn.commit()
    assert find_reset(conn, settings, token) is None        # burned on use
    assert consume_reset(conn, settings, token) is None      # cannot reuse


def test_expired_token_is_invalid(conn, settings):
    u = _user(conn, email="exp@r.com")
    token = create_reset(conn, settings, user_id=u["id"], ttl=timedelta(seconds=-1))
    conn.commit()
    assert find_reset(conn, settings, token) is None


def test_token_is_hashed_at_rest(conn, settings):
    u = _user(conn, email="hash@r.com")
    token = create_reset(conn, settings, user_id=u["id"])
    conn.commit()
    stored = conn.execute("SELECT token_hash FROM password_resets").fetchone()["token_hash"]
    assert token not in stored and stored != token  # only the keyed hash is persisted


# ── HTTP flow ───────────────────────────────────────────────────────────────


def _token_from_outbox(conn, email):
    body = conn.execute("SELECT body FROM emails WHERE to_addr = ?", (email,)).fetchone()["body"]
    return body.split("/reset/")[1].split("\n")[0].strip()


def test_full_reset_flow(client, conn):
    onboard_studio(client, email="reset@me.com", password="oldpassword")
    r = client.post("/forgot", data={"email": "reset@me.com"})
    assert r.status_code == 200 and "on its way" in r.text

    token = _token_from_outbox(conn, "reset@me.com")
    assert client.get(f"/reset/{token}").status_code == 200
    done = client.post(f"/reset/{token}",
                       data={"password": "newpassword1", "confirm": "newpassword1"})
    assert "Password updated" in done.text

    # old password rejected, new password works
    assert "Invalid email or password" in client.post(
        "/login", data={"email": "reset@me.com", "password": "oldpassword"}).text
    ok = client.post("/login", data={"email": "reset@me.com", "password": "newpassword1"})
    assert "/onboarding" in str(ok.url)


def test_forgot_unknown_email_does_not_enumerate(client, conn):
    r = client.post("/forgot", data={"email": "ghost@nowhere.com"})
    assert r.status_code == 200 and "on its way" in r.text          # same response...
    assert conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"] == 0      # ...no mail
    assert conn.execute("SELECT COUNT(*) AS n FROM password_resets").fetchone()["n"] == 0


def test_reset_bad_token_shows_invalid(client):
    page = client.get("/reset/nonsense-token")
    assert page.status_code == 200 and "invalid or has expired" in page.text


def test_reset_rejects_mismatch_without_burning_token(client, conn):
    onboard_studio(client, email="mm@me.com", password="oldpassword")
    client.post("/forgot", data={"email": "mm@me.com"})
    token = _token_from_outbox(conn, "mm@me.com")

    bad = client.post(f"/reset/{token}", data={"password": "longenough", "confirm": "different"})
    assert "Passwords must match" in bad.text
    # token survives a rejected attempt
    assert "invalid or has expired" not in client.get(f"/reset/{token}").text
    good = client.post(f"/reset/{token}",
                       data={"password": "brandnewpass", "confirm": "brandnewpass"})
    assert "Password updated" in good.text


def test_old_sessions_killed_after_reset(client, conn):
    creds = onboard_studio(client, email="sess@me.com", password="oldpassword")
    login_owner(client, creds)                       # establish a live session
    assert client.get("/dashboard").status_code == 200
    token_client = client  # same client holds the session cookie

    client.post("/forgot", data={"email": "sess@me.com"})
    token = _token_from_outbox(conn, "sess@me.com")
    token_client.post(f"/reset/{token}",
                      data={"password": "newpassword1", "confirm": "newpassword1"})
    # the pre-existing session was invalidated → dashboard now bounces to login
    assert token_client.get("/dashboard", follow_redirects=False).status_code == 303


def test_reset_is_audited(client, conn):
    """A password reset is a credential change — it must land in the audit trail."""
    creds = onboard_studio(client, email="audit@me.com", password="oldpassword")
    login_owner(client, creds)
    client.post("/forgot", data={"email": "audit@me.com"})
    token = _token_from_outbox(conn, "audit@me.com")
    client.post(f"/reset/{token}",
                data={"password": "newpassword1", "confirm": "newpassword1"})
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM audit_log WHERE action = 'password.reset'"
    ).fetchone()
    assert row["n"] == 1                                  # attributable credential change

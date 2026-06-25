"""CSRF protection — stateless HMAC token bound to the session cookie.

Enforcement tests use a *plain* ``TestClient`` (not the CSRF-aware fixture) so the
token is omitted unless we add it explicitly — that's how we exercise rejection.
"""

from conftest import login_owner, onboard_studio
from fastapi.testclient import TestClient

from hestia.auth import SESSION_COOKIE
from hestia.csrf import issue_token, valid_token

# ── token primitives ────────────────────────────────────────────────────────


def test_issue_and_verify_roundtrip():
    tok = issue_token("sess-abc", "secret")
    assert tok and valid_token(tok, "sess-abc", "secret")


def test_token_bound_to_session_and_secret():
    tok = issue_token("s1", "secret")
    assert not valid_token(tok, "s2", "secret")   # different session
    assert not valid_token(tok, "s1", "other")    # different secret


def test_empty_inputs_never_validate():
    assert issue_token("", "secret") == ""
    assert not valid_token("", "sess", "secret")
    assert not valid_token("tok", "", "secret")


# ── enforcement on authenticated form POSTs ─────────────────────────────────


def _raw_owner(app):
    creds = onboard_studio(TestClient(app))           # admin client is CSRF-aware
    return login_owner(TestClient(app), creds)        # owner is raw: no auto token


def test_post_without_token_is_forbidden(app):
    owner = _raw_owner(app)
    r = owner.post("/clients", data={"name": "No Token"})
    assert r.status_code == 403


def test_post_with_bad_token_is_forbidden(app):
    owner = _raw_owner(app)
    r = owner.post("/clients", data={"name": "Bad", "csrf_token": "not-the-token"})
    assert r.status_code == 403


def test_post_with_valid_token_succeeds(app, settings):
    owner = _raw_owner(app)
    tok = issue_token(owner.cookies.get(SESSION_COOKIE), settings.session_secret)
    r = owner.post("/clients", data={"name": "Good", "csrf_token": tok},
                   follow_redirects=False)
    assert r.status_code in (302, 303)  # created → redirect, decidedly not 403


def test_get_requests_are_never_blocked(app):
    owner = _raw_owner(app)
    assert owner.get("/clients").status_code == 200


def test_unauthenticated_post_passes_through(client):
    # No session cookie → nothing to forge → CSRF must not apply. /login is under a
    # CSRF-protected router but is reachable logged-out: bad creds → 200 re-render.
    r = client.post("/login", data={"email": "nobody@example.com", "password": "wrong"})
    assert r.status_code == 200  # login page with error, not 403


# ── template wiring ─────────────────────────────────────────────────────────


def test_authenticated_form_embeds_the_token(client, settings):
    creds = onboard_studio(client)
    login_owner(client, creds)
    expected = issue_token(client.cookies.get(SESSION_COOKIE), settings.session_secret)
    page = client.get("/clients/new")
    assert f'name="csrf_token" value="{expected}"' in page.text

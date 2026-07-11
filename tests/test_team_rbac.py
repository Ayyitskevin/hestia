"""Minimal multi-admin RBAC — owner vs secondary admin."""

import dataclasses

from hestia.main import create_app
from hestia.tenants import list_tenant_users
from tests.conftest import CSRFClient, login_owner, onboard_studio

ADMIN_EMAIL = "shooter@studio.com"
ADMIN_PW = "admin-pw-12"


def _client(settings, **overrides):
    app = create_app(dataclasses.replace(settings, **overrides))
    return CSRFClient(app)


def _owner_client(settings):
    c = _client(settings)
    creds = onboard_studio(c)
    login_owner(c, creds)
    return c, creds


def _login(client, email, password):
    """Fresh session on an existing app (a second browser)."""
    client.post("/login", data={"email": email, "password": password})


def _invite_admin(client, email=ADMIN_EMAIL, password=ADMIN_PW):
    return client.post("/settings/team/invite",
                       data={"email": email, "password": password}, follow_redirects=False)


# ── owner team management ────────────────────────────────────────────────────


def test_owner_can_invite_and_remove_admin(settings, db_path):
    c, _ = _owner_client(settings)
    r = _invite_admin(c)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/settings/team?invited=1")

    # team page lists owner + the new admin
    page = c.get("/settings/team").text
    assert ADMIN_EMAIL in page
    assert "Owner" in page

    users = list_tenant_users(_conn(db_path), _tenant_id(c))
    roles = {u["email"]: u["role"] for u in users}
    assert roles[ADMIN_EMAIL] == "admin"
    assert any(v == "owner" for v in roles.values())

    # remove the admin
    admin_id = next(u["id"] for u in users if u["email"] == ADMIN_EMAIL)
    r = c.post(f"/settings/team/{admin_id}/remove", follow_redirects=False)
    assert r.status_code == 303
    remaining = {u["email"] for u in list_tenant_users(_conn(db_path), _tenant_id(c))}
    assert ADMIN_EMAIL not in remaining


def test_owner_cannot_remove_self(settings, db_path):
    c, _ = _owner_client(settings)
    _invite_admin(c)
    users = list_tenant_users(_conn(db_path), _tenant_id(c))
    owner_id = next(u["id"] for u in users if u["role"] == "owner")
    c.post(f"/settings/team/{owner_id}/remove")  # no-op: only admin-role rows delete
    remaining = list_tenant_users(_conn(db_path), _tenant_id(c))
    assert any(u["role"] == "owner" for u in remaining)


def test_invite_rejects_duplicate_and_short_password(settings, db_path):
    c, creds = _owner_client(settings)
    # the owner's own email is already in use
    r = _invite_admin(c, email=creds["email"], password="x" * 12)
    assert r.status_code == 400
    assert "already on this" in r.text.lower()
    # short password
    r = _invite_admin(c, email="new@studio.com", password="short")
    assert r.status_code == 400
    assert "8 characters" in r.text


# ── admin permissions ────────────────────────────────────────────────────────


def test_admin_can_log_in_and_reach_dashboard(settings):
    c, _ = _owner_client(settings)
    _invite_admin(c)
    # second browser, log in as the admin
    admin = CSRFClient(c.app)
    _login(admin, ADMIN_EMAIL, ADMIN_PW)
    r = admin.get("/dashboard", follow_redirects=False)
    assert r.status_code == 200


def test_admin_cannot_access_billing(settings):
    c, _ = _owner_client(settings)
    _invite_admin(c)
    admin = CSRFClient(c.app)
    _login(admin, ADMIN_EMAIL, ADMIN_PW)
    for url in ("/settings/billing", "/settings/account"):
        r = admin.get(url, follow_redirects=False)
        assert r.status_code == 303
        assert "forbidden" in r.headers["location"]


def test_admin_cannot_manage_team(settings):
    c, _ = _owner_client(settings)
    _invite_admin(c)
    admin = CSRFClient(c.app)
    _login(admin, ADMIN_EMAIL, ADMIN_PW)
    # GET team page is owner-only
    r = admin.get("/settings/team", follow_redirects=False)
    assert r.status_code == 303
    assert "forbidden" in r.headers["location"]
    # POSTs are owner-only too
    r = admin.post("/settings/team/invite",
                   data={"email": "sneaky@studio.com", "password": "sneaky-pw-12"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert "forbidden" in r.headers["location"]
    r = admin.post("/settings/team/999/remove", follow_redirects=False)
    assert r.status_code == 303
    assert "forbidden" in r.headers["location"]


def test_admin_cannot_change_plan(settings):
    c, _ = _owner_client(settings)
    _invite_admin(c)
    admin = CSRFClient(c.app)
    _login(admin, ADMIN_EMAIL, ADMIN_PW)
    r = admin.post("/settings/billing/subscribe", data={"plan": "studio"},
                   follow_redirects=False)
    assert r.status_code == 303
    assert "forbidden" in r.headers["location"]


def test_owner_can_access_billing(settings):
    c, _ = _owner_client(settings)
    assert c.get("/settings/billing", follow_redirects=False).status_code == 200
    assert c.get("/settings/account", follow_redirects=False).status_code == 200


# ── helpers ──────────────────────────────────────────────────────────────────


def _conn(db_path):
    from hestia.db import connect

    return connect(db_path)


def _tenant_id(client):
    from hestia.db import get_db

    settings = client.app.state.settings
    with get_db(settings.db_path) as conn:
        row = conn.execute("SELECT id FROM tenants ORDER BY id LIMIT 1").fetchone()
        return row["id"]

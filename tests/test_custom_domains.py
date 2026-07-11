"""Hosted custom-domain readiness."""

import dataclasses

from conftest import ADMIN_TOKEN, CSRFClient, login_owner, onboard_studio

from hestia.db import connect
from hestia.domains import (
    custom_domain_summary,
    normalize_custom_domain,
    set_custom_domain,
    set_custom_domain_status,
    validate_custom_domain,
    verify_custom_domain_dns,
)
from hestia.main import create_app
from hestia.studio import upsert_profile
from hestia.tenants import create_tenant, get_tenant


def test_normalize_and_validate_custom_domain():
    assert normalize_custom_domain(" HTTPS://Photos.Example.COM:443/path?q=1 ") == "photos.example.com"
    assert validate_custom_domain("photos.example.com") is True
    assert validate_custom_domain("localhost") is False
    assert validate_custom_domain("bad_domain.example.com") is False
    assert validate_custom_domain("example.com") is False


def test_set_custom_domain_sets_pending_token_and_reuses_same_domain_token(conn):
    tenant = create_tenant(conn, name="Domain Studio", shoot_type="wedding")
    first = set_custom_domain(conn, tenant["id"], "https://photos.example.co/path")
    second = set_custom_domain(conn, tenant["id"], "photos.example.co")
    conn.commit()

    got = get_tenant(conn, tenant["id"])
    assert first["domain"] == "photos.example.co"
    assert first["status"] == "pending"
    assert first["token"].startswith("hestia-domain-")
    assert second["token"] == first["token"]
    assert got["custom_domain"] == "photos.example.co"
    assert got["custom_domain_status"] == "pending"
    assert got["custom_domain_token"] == first["token"]


def test_custom_domain_must_be_unique_and_not_hosted_subdomain(conn):
    a = create_tenant(conn, name="A", shoot_type="wedding")
    b = create_tenant(conn, name="B", shoot_type="wedding")
    set_custom_domain(conn, a["id"], "brand.example.co")
    conn.commit()

    try:
        set_custom_domain(conn, b["id"], "brand.example.co")
    except ValueError as exc:
        assert "claimed" in str(exc)
    else:
        raise AssertionError("duplicate custom domain accepted")

    try:
        set_custom_domain(conn, b["id"], "b.hestia.test", hosted_domain="hestia.test")
    except ValueError as exc:
        assert "hosted app domain" in str(exc)
    else:
        raise AssertionError("hosted app subdomain accepted as custom domain")


def test_verified_custom_domain_renders_public_studio(settings, conn):
    app = create_app(dataclasses.replace(settings, hosted_domain="hestia.test"))
    client = CSRFClient(app)
    tenant = create_tenant(conn, name="Brand Studio", shoot_type="wedding")
    set_custom_domain(conn, tenant["id"], "brand.example.co")
    set_custom_domain_status(conn, tenant["id"], "verified")
    upsert_profile(conn, tenant_id=tenant["id"], headline="Brand weddings", about="",
                   contact_email="", published=True)
    conn.commit()

    page = client.get("/", headers={"host": "brand.example.co"})
    assert page.status_code == 200
    assert "Brand weddings" in page.text and "Send inquiry" in page.text


def test_pending_custom_domain_does_not_route_public_studio(settings, conn):
    app = create_app(dataclasses.replace(settings, hosted_domain="hestia.test"))
    client = CSRFClient(app)
    tenant = create_tenant(conn, name="Pending Studio", shoot_type="wedding")
    set_custom_domain(conn, tenant["id"], "pending.example.co")
    upsert_profile(conn, tenant_id=tenant["id"], headline="Pending weddings", about="",
                   contact_email="", published=True)
    conn.commit()

    page = client.get("/", headers={"host": "pending.example.co"})
    assert page.status_code == 200
    assert "Gallery to paid" in page.text and "Pending weddings" not in page.text


def test_account_page_saves_custom_domain(settings):
    app = create_app(dataclasses.replace(
        settings,
        public_url="http://app.hestia.test",
        hosted_domain="hestia.test",
    ))
    client = CSRFClient(app)
    creds = onboard_studio(client, name="Domain Account", email="domain@e.com")
    login_owner(client, creds)

    response = client.post(
        "/settings/account/domain",
        data={"custom_domain": "Photos.DomainOwner.COM/path"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get("/settings/account")
    assert "photos.domainowner.com" in page.text
    assert "pending" in page.text
    assert "_hestia.photos.domainowner.com" in page.text
    assert "hestia-domain-" in page.text
    assert "hestia.test" in page.text

    with connect(app.state.settings.db_path) as conn:
        tenant = conn.execute("SELECT * FROM tenants WHERE slug = 'domain-account'").fetchone()
        summary = custom_domain_summary(app.state.settings, dict(tenant))
    assert summary["domain"] == "photos.domainowner.com"
    assert summary["target"] == "hestia.test"


def test_admin_can_verify_and_reset_custom_domain(settings, conn):
    app = create_app(dataclasses.replace(settings, hosted_domain="hestia.test"))
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    tenant = create_tenant(conn, name="Verify Studio", shoot_type="wedding")
    set_custom_domain(conn, tenant["id"], "verify.example.co")
    upsert_profile(conn, tenant_id=tenant["id"], headline="Verified weddings", about="",
                   contact_email="", published=True)
    conn.commit()

    detail = admin.get(f"/admin/tenants/{tenant['id']}")
    assert detail.status_code == 200
    assert "verify.example.co" in detail.text
    assert "_hestia.verify.example.co" in detail.text
    assert "Mark verified" in detail.text

    r = admin.post(f"/admin/tenants/{tenant['id']}/custom-domain/verify",
                   follow_redirects=False)
    assert r.status_code == 303
    got = get_tenant(conn, tenant["id"])
    assert got["custom_domain_status"] == "verified"
    assert "custom_domain.verified" in [
        row["action"] for row in conn.execute(
            "SELECT action FROM audit_log WHERE tenant_id = ?", (tenant["id"],)
        )
    ]

    page = admin.get("/", headers={"host": "verify.example.co"})
    assert "Verified weddings" in page.text

    admin.post(f"/admin/tenants/{tenant['id']}/custom-domain/pending")
    assert get_tenant(conn, tenant["id"])["custom_domain_status"] == "pending"
    detail = admin.get(f"/admin/tenants/{tenant['id']}")
    assert "Reset to pending" not in detail.text and "Mark verified" in detail.text


def test_admin_verify_ignores_empty_custom_domain(settings, conn):
    app = create_app(settings)
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    tenant = create_tenant(conn, name="No Domain", shoot_type="wedding")
    conn.commit()

    r = admin.post(f"/admin/tenants/{tenant['id']}/custom-domain/verify",
                   follow_redirects=False)
    assert r.status_code == 303
    assert get_tenant(conn, tenant["id"])["custom_domain_status"] == "unset"


def test_dns_check_matches_token_and_flips_to_verified(conn):
    """An exact TXT token match auto-verifies — no admin click needed."""
    tenant = create_tenant(conn, name="DNS Studio", shoot_type="wedding")
    result = set_custom_domain(conn, tenant["id"], "photos.example.co")
    token = result["token"]
    conn.commit()

    def resolver(name):
        assert name == "_hestia.photos.example.co"
        return [token]

    out = verify_custom_domain_dns(conn, tenant["id"], resolver=resolver)
    assert out["status"] == "verified" and out["verified"] is True
    assert get_tenant(conn, tenant["id"])["custom_domain_status"] == "verified"


def test_dns_check_no_match_stays_pending(conn):
    tenant = create_tenant(conn, name="No Match", shoot_type="wedding")
    set_custom_domain(conn, tenant["id"], "photos.example.co")
    conn.commit()

    out = verify_custom_domain_dns(conn, tenant["id"], resolver=lambda name: ["wrong"])
    assert out["status"] == "no-match" and out["verified"] is False
    assert get_tenant(conn, tenant["id"])["custom_domain_status"] == "pending"


def test_dns_check_unavailable_when_no_resolver_tool(conn):
    tenant = create_tenant(conn, name="No Tool", shoot_type="wedding")
    set_custom_domain(conn, tenant["id"], "photos.example.co")
    conn.commit()

    out = verify_custom_domain_dns(conn, tenant["id"], resolver=lambda name: None)
    assert out["status"] == "unavailable" and out["verified"] is False
    assert get_tenant(conn, tenant["id"])["custom_domain_status"] == "pending"


def test_dns_check_unset_when_no_domain(conn):
    tenant = create_tenant(conn, name="Empty", shoot_type="wedding")
    conn.commit()
    out = verify_custom_domain_dns(conn, tenant["id"], resolver=lambda name: [])
    assert out["status"] == "unset"


def test_owner_self_serve_dns_check_route_verifies(settings, conn, monkeypatch):
    """The owner's 'Check DNS & verify' button flips the domain to verified when
    the TXT record matches — the founder is out of the loop."""
    app = create_app(dataclasses.replace(settings, hosted_domain="hestia.test"))
    client = CSRFClient(app)
    creds = onboard_studio(client, name="Self Verify", email="self@e.com")
    login_owner(client, creds)
    with connect(app.state.settings.db_path) as db:
        row = db.execute("SELECT id FROM tenants WHERE slug='self-verify'").fetchone()
        res = set_custom_domain(db, row["id"], "self.example.co")
        token = res["token"]
        db.commit()
    monkeypatch.setattr("hestia.domains.resolve_txt_records", lambda name, **_: [token])

    r = client.post("/settings/account/domain/check", follow_redirects=False)
    assert r.status_code == 303 and "dns=verified" in r.headers["location"]
    page = client.get("/settings/account?dns=verified")
    assert "DNS verified" in page.text
    with connect(app.state.settings.db_path) as db:
        status = db.execute("SELECT custom_domain_status FROM tenants WHERE slug='self-verify'").fetchone()[0]
    assert status == "verified"


def test_admin_check_dns_route_verifies_and_audits(settings, conn, monkeypatch):
    app = create_app(dataclasses.replace(settings, hosted_domain="hestia.test"))
    admin = CSRFClient(app)
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    tenant = create_tenant(conn, name="Admin DNS", shoot_type="wedding")
    res = set_custom_domain(conn, tenant["id"], "admin.example.co")
    conn.commit()
    monkeypatch.setattr("hestia.domains.resolve_txt_records", lambda name, **_: [res["token"]])

    r = admin.post(f"/admin/tenants/{tenant['id']}/custom-domain/check-dns",
                   follow_redirects=False)
    assert r.status_code == 303 and "dns=verified" in r.headers["location"]
    assert get_tenant(conn, tenant["id"])["custom_domain_status"] == "verified"
    actions = [row["action"] for row in conn.execute(
        "SELECT action FROM audit_log WHERE tenant_id = ?", (tenant["id"],)
    )]
    assert "custom_domain.verified" in actions

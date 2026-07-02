"""Caddy on-demand TLS gate (/internal/tls-check).

Per-studio subdomains get a certificate the first time they're hit, but Caddy must
ask us first so nobody can make us mint certs for arbitrary hostnames (ACME rate-limit
abuse). We approve the apex, real tenant subdomains, and verified custom domains only.
"""

import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

from hestia.domains import set_custom_domain, set_custom_domain_status
from hestia.main import create_app
from hestia.tenants import create_tenant


def _app(settings):
    return TestClient(create_app(dataclasses.replace(settings, hosted_domain="hestia.test")))


def _check(client, host):
    return client.get("/internal/tls-check", params={"domain": host}).status_code


def test_approves_apex_and_real_tenant_subdomains(settings, conn):
    create_tenant(conn, name="Moonlight Studio", shoot_type="wedding", slug="moonlight")
    conn.commit()
    client = _app(settings)
    assert _check(client, "hestia.test") == 200                 # apex / marketing site
    assert _check(client, "moonlight.hestia.test") == 200       # real tenant subdomain
    assert _check(client, "MOONLIGHT.HESTIA.TEST") == 200       # case-insensitive


def test_refuses_unknown_reserved_and_malformed_subdomains(settings, conn):
    client = _app(settings)
    assert _check(client, "ghost.hestia.test") == 404           # no such tenant
    assert _check(client, "admin.hestia.test") == 404           # reserved subdomain
    assert _check(client, "a.b.hestia.test") == 404             # multi-label slug is invalid
    assert _check(client, "") == 404                            # empty
    assert _check(client, "evil.com") == 404                    # arbitrary host — no cert for you


def test_approves_only_verified_custom_domains(settings, conn):
    verified = create_tenant(conn, name="Brand Studio", shoot_type="wedding")
    set_custom_domain(conn, verified["id"], "brand.example.co")
    set_custom_domain_status(conn, verified["id"], "verified")
    pending = create_tenant(conn, name="Pending Studio", shoot_type="wedding")
    set_custom_domain(conn, pending["id"], "pending.example.co")   # stays pending
    conn.commit()
    client = _app(settings)
    assert _check(client, "brand.example.co") == 200            # DNS-verified → issue
    assert _check(client, "pending.example.co") == 404          # not verified → refuse


def test_caddyfile_uses_on_demand_tls_with_the_ask_gate():
    """Guard the deploy artifact itself: a wildcard cert needs a DNS-01 challenge, so
    the Caddyfile must serve subdomains via on-demand TLS gated by the ask endpoint."""
    caddy = Path("Caddyfile").read_text(encoding="utf-8")
    assert "on_demand_tls" in caddy
    assert "/internal/tls-check" in caddy
    assert "on_demand" in caddy
    assert "*.{$HESTIA_DOMAIN}" in caddy

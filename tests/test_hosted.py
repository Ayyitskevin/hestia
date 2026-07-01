"""Hosted SaaS mode: wildcard tenant subdomains."""

import dataclasses

from conftest import CSRFClient

from hestia.main import create_app
from hestia.studio import upsert_profile
from hestia.tenants import create_tenant


def _hosted_client(settings):
    return CSRFClient(create_app(dataclasses.replace(settings, hosted_domain="hestia.test")))


def test_tenant_subdomain_renders_public_studio(settings, conn):
    app_client = _hosted_client(settings)
    tenant = create_tenant(conn, name="Oak Room", shoot_type="wedding")
    upsert_profile(conn, tenant_id=tenant["id"], headline="Warm weddings", about="",
                   contact_email="", published=True)
    conn.commit()

    page = app_client.get("/", headers={"host": "oak-room.hestia.test"})
    assert page.status_code == 200
    assert "Warm weddings" in page.text and "Send inquiry" in page.text


def test_reserved_subdomain_stays_marketing_app(settings, conn):
    app_client = _hosted_client(settings)
    tenant = create_tenant(conn, name="WWW Studio", slug="www", shoot_type="wedding")
    upsert_profile(conn, tenant_id=tenant["id"], headline="Should not render", about="",
                   contact_email="", published=True)
    conn.commit()

    page = app_client.get("/", headers={"host": "www.hestia.test"})
    assert page.status_code == 200
    assert "Gallery to paid" in page.text and "Should not render" not in page.text


def test_unknown_tenant_subdomain_404s(settings):
    app_client = _hosted_client(settings)
    page = app_client.get("/", headers={"host": "missing.hestia.test"})
    assert page.status_code == 404

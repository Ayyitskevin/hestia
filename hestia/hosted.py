"""Hosted SaaS helpers.

Hestia's canonical public studio URL remains ``/studio/{slug}``, but hosted mode
also supports ``{slug}.{HESTIA_DOMAIN}`` so each tenant gets a premium subdomain
without custom DNS work. The helper is intentionally small: host parsing only;
all tenant lookup still happens through the normal tenant-scoped data access.
"""

from __future__ import annotations

from fastapi import Request

from .domains import get_tenant_by_custom_domain, normalize_custom_domain

RESERVED_SUBDOMAINS = {"admin", "api", "app", "static", "www"}


def host_from_request(request: Request) -> str:
    return request.headers.get("host", "").split(":", 1)[0].strip().lower()


def tenant_slug_from_request(request: Request) -> str | None:
    domain = (getattr(request.app.state.settings, "hosted_domain", "") or "").strip().lower()
    if not domain:
        return None
    host = host_from_request(request)
    if host == domain or not host.endswith(f".{domain}"):
        return None
    slug = host[: -(len(domain) + 1)].strip(".")
    if not slug or "." in slug or slug in RESERVED_SUBDOMAINS:
        return None
    return slug


def tenant_from_custom_domain(conn, request: Request) -> dict | None:
    host = normalize_custom_domain(host_from_request(request))
    if not host:
        return None
    domain = (getattr(request.app.state.settings, "hosted_domain", "") or "").strip().lower()
    if domain and (host == domain or host.endswith(f".{domain}")):
        return None
    public_host = normalize_custom_domain(getattr(request.app.state.settings, "public_url", ""))
    if host == public_host:
        return None
    return get_tenant_by_custom_domain(conn, host)


def tenant_public_url(settings, slug: str) -> str:
    domain = (getattr(settings, "hosted_domain", "") or "").strip().lower()
    if domain:
        return f"https://{slug}.{domain}"
    return f"{settings.public_url.rstrip()}/studio/{slug}"

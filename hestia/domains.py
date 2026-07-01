"""Custom-domain readiness for hosted studios."""

from __future__ import annotations

import re
import sqlite3

from .crypto import new_session_token

_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DOMAIN_STATUSES = ("unset", "pending", "verified")


def normalize_custom_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    raw = raw.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    raw = raw.split("@", 1)[-1].strip().strip(".")
    if ":" in raw:
        raw = raw.split(":", 1)[0]
    return raw


def validate_custom_domain(domain: str) -> bool:
    if not domain or len(domain) > 253 or "." not in domain:
        return False
    if domain in {"localhost", "example.com", "example.org", "example.net"}:
        return False
    labels = domain.split(".")
    return all(_LABEL_RE.match(label) for label in labels)


def custom_domain_target(settings) -> str:
    hosted = (getattr(settings, "hosted_domain", "") or "").strip().strip(".").lower()
    if hosted:
        return hosted
    return normalize_custom_domain(getattr(settings, "public_url", ""))


def custom_domain_dns_name(domain: str) -> str:
    return f"_hestia.{domain}"


def _new_domain_token() -> str:
    return "hestia-domain-" + new_session_token()[:24]


def get_tenant_by_custom_domain(conn: sqlite3.Connection, domain: str) -> dict | None:
    clean = normalize_custom_domain(domain)
    if not clean:
        return None
    row = conn.execute(
        "SELECT * FROM tenants WHERE custom_domain = ? AND custom_domain_status = 'verified'",
        (clean,),
    ).fetchone()
    return dict(row) if row else None


def set_custom_domain(
    conn: sqlite3.Connection,
    tenant_id: str,
    domain: str,
    *,
    hosted_domain: str = "",
) -> dict:
    clean = normalize_custom_domain(domain)
    if not clean:
        conn.execute(
            "UPDATE tenants SET custom_domain = '', custom_domain_status = 'unset', "
            "custom_domain_token = '', custom_domain_updated_at = datetime('now') WHERE id = ?",
            (tenant_id,),
        )
        return {"domain": "", "status": "unset", "token": ""}

    if not validate_custom_domain(clean):
        raise ValueError("invalid custom domain")
    hosted = normalize_custom_domain(hosted_domain)
    if hosted and (clean == hosted or clean.endswith(f".{hosted}")):
        raise ValueError("custom domain must not use the hosted app domain")
    existing = conn.execute(
        "SELECT id FROM tenants WHERE custom_domain = ? AND id != ? LIMIT 1",
        (clean, tenant_id),
    ).fetchone()
    if existing:
        raise ValueError("custom domain already claimed")
    current = conn.execute(
        "SELECT custom_domain, custom_domain_token FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    token = (
        current["custom_domain_token"]
        if current and current["custom_domain"] == clean and current["custom_domain_token"]
        else _new_domain_token()
    )
    conn.execute(
        "UPDATE tenants SET custom_domain = ?, custom_domain_status = 'pending', "
        "custom_domain_token = ?, custom_domain_updated_at = datetime('now') WHERE id = ?",
        (clean, token, tenant_id),
    )
    return {"domain": clean, "status": "pending", "token": token}


def set_custom_domain_status(conn: sqlite3.Connection, tenant_id: str, status: str) -> None:
    if status not in DOMAIN_STATUSES:
        raise ValueError("invalid custom domain status")
    if status == "verified":
        row = conn.execute(
            "SELECT custom_domain FROM tenants WHERE id = ? AND custom_domain <> ''",
            (tenant_id,),
        ).fetchone()
        if not row:
            raise ValueError("cannot verify an empty custom domain")
    conn.execute(
        "UPDATE tenants SET custom_domain_status = ?, custom_domain_updated_at = datetime('now') "
        "WHERE id = ?",
        (status, tenant_id),
    )


def custom_domain_summary(settings, tenant: dict) -> dict:
    domain = tenant.get("custom_domain") or ""
    token = tenant.get("custom_domain_token") or ""
    return {
        "domain": domain,
        "status": tenant.get("custom_domain_status") or "unset",
        "token": token,
        "dns_name": custom_domain_dns_name(domain) if domain else "",
        "target": custom_domain_target(settings),
        "url": f"https://{domain}" if domain and tenant.get("custom_domain_status") == "verified" else "",
    }

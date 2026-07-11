"""Custom-domain readiness for hosted studios."""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess

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


_TXT_QUOTED_RE = re.compile(r'"([^"]*)"')


def _stdlib_txt_records(name: str) -> list[str] | None:
    """Best-effort TXT lookup via dig or nslookup. Returns the TXT values found,
    or None when no resolver tool is available on the host (so callers can
    distinguish "no match" from "couldn't check")."""
    if shutil.which("dig"):
        proc = subprocess.run(  # noqa: S603 - bounded, host-provided tool
            ["dig", "+short", "+time=3", "+tries=1", "TXT", name],
            capture_output=True, text=True, timeout=8,
        )
        return _TXT_QUOTED_RE.findall(proc.stdout)
    if shutil.which("nslookup"):
        proc = subprocess.run(  # noqa: S603 - bounded, host-provided tool
            ["nslookup", "-timeout=3", "-type=TXT", name],
            capture_output=True, text=True, timeout=8,
        )
        return _TXT_QUOTED_RE.findall(proc.stdout)
    return None


def resolve_txt_records(name: str, *, resolver=None) -> list[str] | None:
    """Resolve TXT records for ``name``. ``resolver`` is an injectable seam
    (callable name -> list[str] | None) so tests are deterministic; the default
    shells out to dig/nslookup and degrades to None when neither is installed."""
    if resolver is not None:
        return resolver(name)
    return _stdlib_txt_records(name)


def verify_custom_domain_dns(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    resolver=None,
) -> dict:
    """Check the studio's verification TXT record and flip to verified on match.

    The owner publishes ``TXT _hestia.<domain> = <token>``. This reads the live
    record, and on an exact token match marks the domain verified — no admin
    click needed. Returns a result dict so the route can banner the outcome.
    """
    row = conn.execute(
        "SELECT custom_domain, custom_domain_token, custom_domain_status "
        "FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    if not row or not row["custom_domain"] or not row["custom_domain_token"]:
        return {"verified": False, "status": "unset",
                "reason": "no custom domain to verify"}
    domain = row["custom_domain"]
    token = row["custom_domain_token"]
    found = resolve_txt_records(custom_domain_dns_name(domain), resolver=resolver)
    if found is None:
        return {"verified": False, "status": "unavailable",
                "reason": "no DNS resolver tool (dig/nslookup) on the host — use Mark verified"}
    if token in found:
        set_custom_domain_status(conn, tenant_id, "verified")
        return {"verified": True, "status": "verified",
                "reason": f"TXT record for {custom_domain_dns_name(domain)} matched the token"}
    return {"verified": False, "status": "no-match",
            "reason": f"TXT record for {custom_domain_dns_name(domain)} not found or does not match"}


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

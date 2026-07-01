"""Studio (tenant), user, and API-key data access.

A *tenant* is one photography studio. It has users (email/password), a
``shoot_type`` that tunes offer/album defaults, and optional ``hestia_tk_*`` API
keys for automation. Secrets are stored hashed (see :mod:`hestia.crypto`).
"""

from __future__ import annotations

import re
import sqlite3
import uuid

from .config import Settings
from .crypto import (
    generate_tenant_api_key,
    hash_api_key,
    hash_password,
    parse_tenant_slug,
    verify_api_key,
)
from .features import flags_for, normalize_shoot_type

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SIGNUP_SOURCE_RE = re.compile(r"[^a-z0-9_-]+")
_SIGNUP_SOURCES = {"landing", "pricing", "demo", "beta", "interest"}
_SIGNUP_PATHS = {
    "/",
    "/pricing",
    "/demo",
    "/demo/wedding",
    "/demo/food",
    "/demo/real-estate",
    "/beta",
    "/interest",
}


def slugify(value: str) -> str:
    slug = _SLUG_RE.sub("-", value.strip().lower()).strip("-")
    return slug or "studio"


def signup_attribution(source: str | None, landing_path: str | None) -> dict:
    """Sanitize first-party signup attribution before it reaches tenant storage."""
    path = _normalize_signup_path(landing_path)
    source_norm = _SIGNUP_SOURCE_RE.sub("-", (source or "").strip().lower()).strip("-_")[:40]
    if source_norm not in _SIGNUP_SOURCES:
        source_norm = _source_from_path(path)
    return {"source": source_norm, "landing_path": path}


def _normalize_signup_path(value: str | None) -> str:
    raw = (value or "").strip().split("?", 1)[0].split("#", 1)[0]
    if not raw.startswith("/") or raw.startswith("//"):
        return ""
    raw = raw[:80]
    return raw if raw in _SIGNUP_PATHS else ""


def _source_from_path(path: str) -> str:
    if path == "/pricing":
        return "pricing"
    if path.startswith("/demo"):
        return "demo"
    if path == "/":
        return "landing"
    if path == "/beta":
        return "beta"
    if path == "/interest":
        return "interest"
    return ""


# ── Tenants (studios) ───────────────────────────────────────────────────────


def create_tenant(
    conn: sqlite3.Connection,
    *,
    name: str,
    shoot_type: str,
    slug: str | None = None,
    plan: str = "beta",
    signup_source: str | None = None,
    signup_landing_path: str | None = None,
) -> dict:
    tenant_id = uuid.uuid4().hex
    base_slug = slugify(slug or name)
    slug_final = base_slug
    n = 2
    while conn.execute("SELECT 1 FROM tenants WHERE slug = ?", (slug_final,)).fetchone():
        slug_final = f"{base_slug}-{n}"
        n += 1
    attribution = signup_attribution(signup_source, signup_landing_path)
    conn.execute(
        "INSERT INTO tenants "
        "(id, slug, name, shoot_type, plan, signup_source, signup_landing_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            tenant_id,
            slug_final,
            name,
            normalize_shoot_type(shoot_type),
            plan,
            attribution["source"],
            attribution["landing_path"],
        ),
    )
    return get_tenant(conn, tenant_id)


def get_tenant(conn: sqlite3.Connection, tenant_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def get_tenant_by_slug(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM tenants WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def set_tax_rate(conn: sqlite3.Connection, tenant_id: str, tax_rate_bps: int) -> None:
    """Set a studio's sales-tax rate in basis points (850 = 8.50%), clamped 0–100%."""
    bps = max(0, min(10000, int(tax_rate_bps)))
    conn.execute("UPDATE tenants SET tax_rate_bps = ? WHERE id = ?", (bps, tenant_id))


def set_booking_rules(conn: sqlite3.Connection, tenant_id: str, *, min_notice_hours: int,
                      buffer_minutes: int) -> None:
    """Set the self-serve booking guardrails (both clamped to sane bounds): minimum notice
    in hours and the buffer kept clear around each session in minutes."""
    notice = max(0, min(24 * 30, int(min_notice_hours)))     # up to ~a month out
    buffer_min = max(0, min(24 * 60, int(buffer_minutes)))    # up to a day
    conn.execute(
        "UPDATE tenants SET booking_min_notice_hours = ?, booking_buffer_minutes = ? WHERE id = ?",
        (notice, buffer_min, tenant_id),
    )


def list_tenants(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM tenants ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_shoot_type(conn: sqlite3.Connection, tenant_id: str, shoot_type: str) -> None:
    conn.execute(
        "UPDATE tenants SET shoot_type = ? WHERE id = ?",
        (normalize_shoot_type(shoot_type), tenant_id),
    )


def tenant_flags(tenant: dict):
    return flags_for(tenant.get("shoot_type"))


# The flat $40 plan includes the premium AI style profile; no paid upsell tier.
STYLE_PROFILE_PLANS = ("beta", "studio", "studio_pro")


def can_use_style_profile(tenant: dict) -> bool:
    return tenant.get("plan") in STYLE_PROFILE_PLANS


def set_vision_style(conn: sqlite3.Connection, tenant_id: str, style: str) -> None:
    conn.execute(
        "UPDATE tenants SET vision_style = ? WHERE id = ?", (style.strip()[:500], tenant_id)
    )


def set_email_signature(conn: sqlite3.Connection, tenant_id: str, signature: str) -> None:
    """Set the studio's email signature — free text appended to client-facing mail."""
    conn.execute(
        "UPDATE tenants SET email_signature = ? WHERE id = ?", (signature.strip()[:600], tenant_id)
    )


# ── Users ───────────────────────────────────────────────────────────────────


def create_user(
    conn: sqlite3.Connection,
    *,
    tenant_id: str | None,
    email: str,
    password: str,
    role: str = "owner",
    verified: int = 1,
) -> dict:
    cur = conn.execute(
        "INSERT INTO users (tenant_id, email, password_hash, role, verified) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, email.strip().lower(), hash_password(password), role, verified),
    )
    row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def mark_user_verified(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute("UPDATE users SET verified = 1 WHERE id = ?", (user_id,))


def get_user_by_email(conn: sqlite3.Connection, email: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM users WHERE email = ? ORDER BY id LIMIT 1", (email.strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


def set_user_password(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
    )


def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


# ── Tenant API keys (hestia_tk_<slug>_<secret>) ─────────────────────────────


def create_tenant_api_key(
    conn: sqlite3.Connection, settings: Settings, tenant_id: str
) -> str:
    tenant = get_tenant(conn, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    api_key = generate_tenant_api_key(tenant["slug"])
    token_hash = hash_api_key(api_key, settings.tenant_key_pepper)
    prefix = api_key[: api_key.rfind("_") + 5] + "…"
    conn.execute(
        "INSERT INTO tenant_api_keys (tenant_id, token_hash, prefix) VALUES (?, ?, ?)",
        (tenant_id, token_hash, prefix),
    )
    return api_key


def find_tenant_by_api_key(
    conn: sqlite3.Connection, settings: Settings, api_key: str
) -> dict | None:
    slug = parse_tenant_slug(api_key)
    if not slug:
        return None
    tenant = get_tenant_by_slug(conn, slug)
    if not tenant:
        return None
    rows = conn.execute(
        "SELECT token_hash FROM tenant_api_keys WHERE tenant_id = ?", (tenant["id"],)
    ).fetchall()
    for r in rows:
        if verify_api_key(api_key, r["token_hash"], settings.tenant_key_pepper):
            return tenant
    return None

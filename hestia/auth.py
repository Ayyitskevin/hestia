"""Authentication: UI session cookies and API bearer tokens.

Two ways in, mirroring the Plutus pattern:

- **UI** — email/password login mints a row in ``sessions`` and sets the
  ``hestia_session`` cookie. Admins log in with the master ``HESTIA_API_TOKEN``.
- **API** — ``Authorization: Bearer hestia_tk_<slug>_<secret>`` resolves to a
  tenant; the master ``HESTIA_API_TOKEN`` authenticates admin endpoints.
"""

from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import Request

from .config import Settings
from .crypto import needs_rehash, new_session_token, verify_password
from .tenants import find_tenant_by_api_key, get_tenant, get_user_by_email, set_user_password

SESSION_COOKIE = "hestia_session"
SESSION_TTL = timedelta(hours=12)

# Per-tenant roles. ``owner`` is the account holder — billing, the plan, the
# danger zone (integrity repair), bring-your-own AI keys, and managing other
# admins. ``admin`` is a secondary studio admin who can run the studio
# (galleries, clients, pipeline, non-billing settings) but not change the plan
# or manage the team. Enforced at the route seam.
OWNER = "owner"
ADMIN = "admin"
ROLES = (OWNER, ADMIN)


@dataclass
class AuthContext:
    kind: str  # "admin" | "user"
    role: str
    user: dict | None = None
    tenant: dict | None = None

    @property
    def is_admin(self) -> bool:
        return self.kind == "admin"

    @property
    def is_owner(self) -> bool:
        """A tenant user acting as the account owner (not the founder admin)."""
        return self.kind == "user" and self.role == OWNER

    @property
    def tenant_id(self) -> str | None:
        return self.tenant["id"] if self.tenant else None


# ── Session lifecycle ───────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def create_session(
    conn: sqlite3.Connection,
    *,
    role: str,
    user_id: int | None = None,
    tenant_id: str | None = None,
    ttl: timedelta = SESSION_TTL,
) -> str:
    token = new_session_token()
    expires = (_now() + ttl).isoformat()
    conn.execute(
        "INSERT INTO sessions (token, user_id, tenant_id, role, expires_at) VALUES (?, ?, ?, ?, ?)",
        (token, user_id, tenant_id, role, expires),
    )
    return token


def get_valid_session(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < _now():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None
    return dict(row)


def destroy_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def authenticate_user(conn: sqlite3.Connection, email: str, password: str) -> dict | None:
    user = get_user_by_email(conn, email)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    # Upgrade-on-login: now that we hold the plaintext, transparently re-hash any
    # password stored under a weaker KDF cost at the current work factor. Zero user
    # impact; the fleet migrates to the stronger hash as people sign in.
    if needs_rehash(user["password_hash"]):
        set_user_password(conn, user["id"], password)
    return user


# ── Request → AuthContext resolution ────────────────────────────────────────


def context_from_session(conn: sqlite3.Connection, request: Request) -> AuthContext | None:
    token = request.cookies.get(SESSION_COOKIE)
    session = get_valid_session(conn, token)
    if not session:
        return None
    # A founder-admin session carries no user or tenant; a studio-user session
    # always carries both. Distinguish on that, not on the role string — a
    # secondary studio admin (role "admin") is a user, not the founder admin,
    # and must resolve through the tenant path below.
    if session["user_id"] is None and session["tenant_id"] is None:
        if session["role"] == "admin":
            return AuthContext(kind="admin", role="admin")
        destroy_session(conn, token)
        return None
    if not session["user_id"] or not session["tenant_id"]:
        destroy_session(conn, token)
        return None
    tenant = get_tenant(conn, session["tenant_id"]) if session["tenant_id"] else None
    if not tenant:
        return None
    user = None
    if session["user_id"]:
        row = conn.execute(
            "SELECT * FROM users WHERE id = ? AND tenant_id = ?",
            (session["user_id"], session["tenant_id"]),
        ).fetchone()
        if not row or row["role"] != session["role"]:
            # A user session is valid only while its user, tenant, and role still
            # agree. Fail closed on stale/corrupt rows instead of granting the
            # tenant access encoded in the session record alone.
            destroy_session(conn, token)
            return None
        user = dict(row)
    return AuthContext(kind="user", role=session["role"], user=user, tenant=tenant)


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def context_from_bearer(
    conn: sqlite3.Connection, settings: Settings, request: Request
) -> AuthContext | None:
    token = _bearer(request)
    if not token:
        return None
    # Admin master token.
    if settings.api_token and hmac.compare_digest(token, settings.api_token):
        return AuthContext(kind="admin", role="admin")
    # Per-tenant API key.
    tenant = find_tenant_by_api_key(conn, settings, token)
    if tenant:
        return AuthContext(kind="user", role="owner", tenant=tenant)
    return None


def resolve_context(
    conn: sqlite3.Connection, settings: Settings, request: Request
) -> AuthContext | None:
    """Resolve auth from bearer header first, then session cookie."""
    return context_from_bearer(conn, settings, request) or context_from_session(
        conn, request
    )


def cookie_is_secure(settings: Settings) -> bool:
    return settings.public_url.lower().startswith("https://")

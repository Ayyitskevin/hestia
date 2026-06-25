"""Password reset — single-use, expiring tokens, hashed at rest.

A reset token is random and shown exactly once (in the emailed link); only its
keyed hash is stored (same treatment as tenant API keys), so a database leak can't
be turned into a working reset. Tokens expire and are burned on use. Callers must
not leak whether an email matched — no user enumeration.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .config import Settings
from .crypto import hash_api_key, new_session_token

RESET_TTL = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(UTC)


def create_reset(conn: sqlite3.Connection, settings: Settings, *, user_id: int,
                 ttl: timedelta = RESET_TTL) -> str:
    token = new_session_token()
    token_hash = hash_api_key(token, settings.tenant_key_pepper)
    expires = (_now() + ttl).isoformat()
    conn.execute(
        "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
        (user_id, token_hash, expires),
    )
    return token


def find_reset(conn: sqlite3.Connection, settings: Settings, token: str) -> dict | None:
    """Return the live reset row for a token (exists, unused, unexpired), else None."""
    if not token:
        return None
    token_hash = hash_api_key(token, settings.tenant_key_pepper)
    row = conn.execute(
        "SELECT * FROM password_resets WHERE token_hash = ?", (token_hash,)
    ).fetchone()
    if not row or row["used_at"]:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires < _now():
        return None
    return dict(row)


def consume_reset(conn: sqlite3.Connection, settings: Settings, token: str) -> int | None:
    """Validate and burn a token. Returns the user_id, or None if invalid/expired/used."""
    row = find_reset(conn, settings, token)
    if not row:
        return None
    conn.execute(
        "UPDATE password_resets SET used_at = datetime('now') WHERE id = ?", (row["id"],)
    )
    return row["user_id"]

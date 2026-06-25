"""Email verification — single-use, expiring tokens for self-serve signup.

Same token treatment as password reset (:mod:`hestia.resets`): random token,
keyed-hash at rest, expiry, single-use. Consuming a valid token returns the
user_id so the caller can flip the account to verified.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from .config import Settings
from .crypto import hash_api_key, new_session_token

VERIFY_TTL = timedelta(days=2)


def _now() -> datetime:
    return datetime.now(UTC)


def create_verification(conn: sqlite3.Connection, settings: Settings, *, user_id: int,
                        ttl: timedelta = VERIFY_TTL) -> str:
    token = new_session_token()
    token_hash = hash_api_key(token, settings.tenant_key_pepper)
    conn.execute(
        "INSERT INTO email_verifications (user_id, token_hash, expires_at) VALUES (?, ?, ?)",
        (user_id, token_hash, (_now() + ttl).isoformat()),
    )
    return token


def consume_verification(conn: sqlite3.Connection, settings: Settings, token: str) -> int | None:
    """Validate and burn a verification token. Returns the user_id, or None."""
    if not token:
        return None
    token_hash = hash_api_key(token, settings.tenant_key_pepper)
    row = conn.execute(
        "SELECT * FROM email_verifications WHERE token_hash = ?", (token_hash,)
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
    conn.execute(
        "UPDATE email_verifications SET used_at = datetime('now') WHERE id = ?", (row["id"],)
    )
    return row["user_id"]

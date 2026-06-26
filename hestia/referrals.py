"""Referrals — turn a past client into the next booking's source.

Each client has a lazily-minted, unguessable referral code. Sharing
``/studio/{slug}?ref={code}`` tags any inquiry that arrives through it back to the
referring client, so the studio can see which past clients drive new business.
Pure attribution: no rewards engine, and it rides the existing public inquiry POST
rather than adding a new write surface.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .crm import get_client
from .crypto import new_session_token


def referral_code_for(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> str | None:
    """The client's referral code, minting one on first use. Idempotent — an
    existing code is preserved so a shared link keeps working."""
    client = get_client(conn, tenant_id, client_id)
    if not client:
        return None
    if client.get("referral_code"):
        return client["referral_code"]
    code = new_session_token()
    conn.execute(
        "UPDATE clients SET referral_code = ? WHERE id = ? AND tenant_id = ?",
        (code, client_id, tenant_id),
    )
    return code


def client_by_referral_code(conn: sqlite3.Connection, tenant_id: str, code: str) -> dict | None:
    """Resolve a referral code to its client within a tenant. A blank code never
    matches (every code-less client stores ''), so it can't leak a default row."""
    if not code:
        return None
    row = conn.execute(
        "SELECT * FROM clients WHERE tenant_id = ? AND referral_code = ?",
        (tenant_id, code),
    ).fetchone()
    return dict(row) if row else None


def attribute_referral(conn: sqlite3.Connection, tenant_id: str, project_id: int,
                       code: str) -> int | None:
    """Tag a project (lead) with the client whose referral code brought it in.
    No-op when the code is blank or unknown. Returns the referrer's id, or None."""
    referrer = client_by_referral_code(conn, tenant_id, code)
    if not referrer:
        return None
    conn.execute(
        "UPDATE projects SET referred_by_client_id = ? WHERE id = ? AND tenant_id = ?",
        (referrer["id"], project_id, tenant_id),
    )
    return referrer["id"]


def referral_link(settings: Settings, slug: str, code: str) -> str:
    return f"{settings.public_url.rstrip('/')}/studio/{slug}?ref={code}"

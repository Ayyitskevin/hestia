"""Referral rewards — when a referred lead books, the referrer earns a credit.

Completes the word-of-mouth loop started by :mod:`hestia.referrals`: that module
attributes an inquiry to the client who referred it; this one pays that client a
flat reward the moment their referral converts (the project reaches ``booked``).
Credits accrue per converted project (one each, enforced by a UNIQUE constraint),
and the owner redeems them manually — no entanglement with the payments rail.
"""

from __future__ import annotations

import sqlite3


def award_referral_credit(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> int | None:
    """Grant the referring client the studio's reward for a converted referral.

    Idempotent: a UNIQUE(project_id) plus INSERT OR IGNORE means a re-book (or any
    second ``booked`` transition) never double-credits. A no-op — returning None —
    when the project wasn't referred, was already credited, or the reward is zero."""
    proj = conn.execute(
        "SELECT referred_by_client_id FROM projects WHERE id = ? AND tenant_id = ?",
        (project_id, tenant_id),
    ).fetchone()
    if not proj or not proj["referred_by_client_id"]:
        return None
    row = conn.execute(
        "SELECT referral_reward_cents FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    amount = int(row["referral_reward_cents"]) if row else 0
    if amount <= 0:
        return None
    cur = conn.execute(
        "INSERT OR IGNORE INTO referral_credits (tenant_id, client_id, project_id, amount_cents) "
        "VALUES (?, ?, ?, ?)",
        (tenant_id, proj["referred_by_client_id"], project_id, amount),
    )
    return cur.lastrowid if cur.rowcount else None


def list_credits(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> list[dict]:
    """A client's referral credits, newest first (for the CRM view)."""
    rows = conn.execute(
        "SELECT * FROM referral_credits WHERE tenant_id = ? AND client_id = ? ORDER BY id DESC",
        (tenant_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]


def credit_balance(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> int:
    """Unredeemed referral credit (cents) a client has earned."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM referral_credits "
        "WHERE tenant_id = ? AND client_id = ? AND status = 'earned'",
        (tenant_id, client_id),
    ).fetchone()
    return int(row["total"])


def redeem_credit(conn: sqlite3.Connection, tenant_id: str, credit_id: int) -> bool:
    """Mark a credit redeemed (the owner applied it to an order/invoice by hand).
    Idempotent — only an 'earned' credit moves, so a double-click is a no-op."""
    cur = conn.execute(
        "UPDATE referral_credits SET status = 'redeemed', redeemed_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status = 'earned'",
        (credit_id, tenant_id),
    )
    return cur.rowcount > 0

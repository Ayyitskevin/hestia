"""Mini-session drops: fixed slots that sell through the normal booking flow."""

from __future__ import annotations

import sqlite3

from .booking import request_booking
from .config import Settings
from .db import audit
from .hosted import tenant_public_url
from .invoices import money
from .tenants import slugify

MINI_SESSION_STATUSES = ("draft", "published", "archived")
MAX_DURATION_MINUTES = 24 * 60


def _clean_status(status: str) -> str:
    return status if status in MINI_SESSION_STATUSES else "draft"


def _clean_duration(value: int | str | None) -> int:
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        raw = 20
    return min(MAX_DURATION_MINUTES, max(1, raw))


def _unique_slug(conn: sqlite3.Connection, tenant_id: str, title: str) -> str:
    base = slugify(title)
    slug = base
    n = 2
    while conn.execute(
        "SELECT 1 FROM mini_sessions WHERE tenant_id = ? AND slug = ?",
        (tenant_id, slug),
    ).fetchone():
        slug = f"{base}-{n}"
        n += 1
    return slug


def create_mini_session(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    title: str,
    description: str = "",
    duration_minutes: int = 20,
    price_cents: int = 0,
    deposit_cents: int = 0,
) -> dict | None:
    title = (title or "").strip()
    if not title:
        return None
    slug = _unique_slug(conn, tenant_id, title)
    cur = conn.execute(
        """
        INSERT INTO mini_sessions
            (tenant_id, slug, title, description, duration_minutes, price_cents, deposit_cents)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            slug,
            title[:200],
            (description or "").strip()[:2000],
            _clean_duration(duration_minutes),
            max(0, int(price_cents or 0)),
            max(0, int(deposit_cents or 0)),
        ),
    )
    audit(conn, actor="owner", action="mini_session.created", tenant_id=tenant_id, detail=title[:200])
    return get_mini_session(conn, tenant_id, cur.lastrowid)


def get_mini_session(conn: sqlite3.Connection, tenant_id: str, session_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM mini_sessions WHERE id = ? AND tenant_id = ?",
        (session_id, tenant_id),
    ).fetchone()
    return _hydrate_drop(conn, dict(row)) if row else None


def get_mini_session_by_slug(
    conn: sqlite3.Connection,
    tenant_id: str,
    slug: str,
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM mini_sessions WHERE tenant_id = ? AND slug = ?",
        (tenant_id, slug),
    ).fetchone()
    return _hydrate_drop(conn, dict(row)) if row else None


def list_mini_sessions(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mini_sessions WHERE tenant_id = ? "
        "ORDER BY CASE status WHEN 'published' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, "
        "created_at DESC, id DESC",
        (tenant_id,),
    ).fetchall()
    return [_hydrate_drop(conn, dict(row)) for row in rows]


def list_published_mini_sessions(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM mini_sessions WHERE tenant_id = ? AND status = 'published' "
        "ORDER BY created_at DESC, id DESC",
        (tenant_id,),
    ).fetchall()
    return [_hydrate_drop(conn, dict(row)) for row in rows]


def set_mini_session_status(
    conn: sqlite3.Connection,
    tenant_id: str,
    session_id: int,
    status: str,
) -> bool:
    clean = _clean_status(status)
    cur = conn.execute(
        "UPDATE mini_sessions SET status = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (clean, session_id, tenant_id),
    )
    if cur.rowcount:
        audit(
            conn,
            actor="owner",
            action=f"mini_session.{clean}",
            tenant_id=tenant_id,
            detail=str(session_id),
        )
    return cur.rowcount == 1


def add_mini_session_slots(
    conn: sqlite3.Connection,
    tenant_id: str,
    session_id: int,
    starts_at_lines: str,
) -> int:
    drop = get_mini_session(conn, tenant_id, session_id)
    if not drop:
        return 0
    added = 0
    for raw in starts_at_lines.splitlines():
        starts_at = raw.replace("T", " ").strip()
        if not starts_at:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO mini_session_slots (tenant_id, mini_session_id, starts_at) "
            "VALUES (?, ?, ?)",
            (tenant_id, session_id, starts_at[:80]),
        )
        added += int(cur.rowcount == 1)
    if added:
        audit(conn, actor="owner", action="mini_session.slots_added",
              tenant_id=tenant_id, detail=f"{session_id}:{added}")
    return added


def delete_open_slot(conn: sqlite3.Connection, tenant_id: str, slot_id: int) -> bool:
    cur = conn.execute(
        "DELETE FROM mini_session_slots WHERE id = ? AND tenant_id = ? AND status = 'open'",
        (slot_id, tenant_id),
    )
    return cur.rowcount == 1


def list_mini_session_slots(
    conn: sqlite3.Connection,
    tenant_id: str,
    session_id: int,
    *,
    public_only: bool = False,
) -> list[dict]:
    sql = (
        "SELECT s.*, c.name AS client_name, c.email AS client_email "
        "FROM mini_session_slots s "
        "LEFT JOIN clients c ON c.id = s.client_id AND c.tenant_id = s.tenant_id "
        "WHERE s.tenant_id = ? AND s.mini_session_id = ?"
    )
    params: list = [tenant_id, session_id]
    if public_only:
        sql += " AND s.status = 'open'"
    sql += " ORDER BY datetime(s.starts_at), s.starts_at, s.id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def claim_mini_session_slot(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant: dict,
    drop: dict,
    slot_id: int,
    name: str,
    email: str,
    message: str = "",
) -> dict | None:
    """Claim one open slot and create the normal confirmed booking artifacts.

    The caller should hold SQLite's write lock (``BEGIN IMMEDIATE``) in HTTP flows.
    The guarded slot lookup still keeps direct callers tenant/drop/status scoped.
    """
    slot = conn.execute(
        "SELECT * FROM mini_session_slots "
        "WHERE id = ? AND tenant_id = ? AND mini_session_id = ? AND status = 'open'",
        (slot_id, tenant["id"], drop["id"]),
    ).fetchone()
    if not slot:
        return None
    cur = conn.execute(
        "UPDATE mini_session_slots SET status = 'claimed', claimed_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND mini_session_id = ? AND status = 'open'",
        (slot_id, tenant["id"], drop["id"]),
    )
    if cur.rowcount != 1:
        return None
    booking_type = {
        "title": drop["title"],
        "kind": "shoot",
        "duration_minutes": drop["duration_minutes"],
        "price_cents": drop["price_cents"],
        "deposit_cents": drop["deposit_cents"],
    }
    result = request_booking(
        conn,
        settings,
        tenant=tenant,
        booking_type=booking_type,
        name=name,
        email=email,
        requested_at=slot["starts_at"],
        message=message,
        lead_source="mini_session",
        confirm=True,
    )
    cur = conn.execute(
        "UPDATE mini_session_slots "
        "SET client_id = ?, project_id = ?, appointment_id = ?, invoice_id = ? "
        "WHERE id = ? AND tenant_id = ? AND mini_session_id = ? AND status = 'claimed'",
        (
            result["client"]["id"],
            result["project"]["id"],
            result["appointment"]["id"],
            result["invoice"]["id"] if result["invoice"] else None,
            slot_id,
            tenant["id"],
            drop["id"],
        ),
    )
    if cur.rowcount != 1:
        return None
    audit(conn, actor="public", action="mini_session.claimed",
          tenant_id=tenant["id"], detail=f"{drop['title']} · {slot['starts_at']} · {email}")
    result["slot"] = dict(slot)
    return result


def mini_session_public_url(settings: Settings, tenant_slug: str, drop_slug: str) -> str:
    base = tenant_public_url(settings, tenant_slug)
    if base.rstrip("/").endswith(f"/studio/{tenant_slug}"):
        return f"{base.rstrip('/')}/mini-sessions/{drop_slug}"
    return f"{settings.public_url.rstrip('/')}/studio/{tenant_slug}/mini-sessions/{drop_slug}"


def hydrate_mini_session_displays(
    settings: Settings,
    tenant_slug: str,
    drops: list[dict],
) -> list[dict]:
    for drop in drops:
        drop["price_display"] = money(drop["price_cents"], settings.currency) if drop["price_cents"] else ""
        drop["deposit_display"] = (
            money(drop["deposit_cents"], settings.currency) if drop["deposit_cents"] else ""
        )
        drop["public_url"] = mini_session_public_url(settings, tenant_slug, drop["slug"])
    return drops


def _hydrate_drop(conn: sqlite3.Connection, drop: dict) -> dict:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS slot_count,
            COALESCE(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END), 0) AS open_count,
            COALESCE(SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END), 0) AS claimed_count
          FROM mini_session_slots
         WHERE tenant_id = ? AND mini_session_id = ?
        """,
        (drop["tenant_id"], drop["id"]),
    ).fetchone()
    drop["slot_count"] = int(row["slot_count"] or 0)
    drop["open_count"] = int(row["open_count"] or 0)
    drop["claimed_count"] = int(row["claimed_count"] or 0)
    return drop

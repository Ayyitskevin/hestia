"""CRM module — clients and projects (the studio-OS backbone, essence of Mise).

This is the connective tissue of the studio OS: galleries (and invoices, albums,
and campaigns) hang off a project, and projects belong to a client. Pure data
access, tenant-scoped throughout.

    client → project (shoot_type, status, event_date) → gallery → offer
"""

from __future__ import annotations

import sqlite3

from .automations import emit_event
from .features import normalize_shoot_type

PROJECT_STATUSES = ("lead", "booked", "shooting", "delivered", "archived")


# ── Clients ─────────────────────────────────────────────────────────────────


def create_client(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    name: str,
    email: str = "",
    phone: str = "",
    notes: str = "",
) -> dict:
    cur = conn.execute(
        "INSERT INTO clients (tenant_id, name, email, phone, notes) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, name.strip(), email.strip(), phone.strip(), notes.strip()),
    )
    return get_client(conn, tenant_id, cur.lastrowid)


def get_client(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM clients WHERE id = ? AND tenant_id = ?", (client_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_clients(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.*, COUNT(p.id) AS project_count
          FROM clients c
          LEFT JOIN projects p ON p.client_id = c.id
         WHERE c.tenant_id = ?
         GROUP BY c.id
         ORDER BY c.created_at DESC
        """,
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Projects ────────────────────────────────────────────────────────────────


def create_project(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    name: str,
    client_id: int | None = None,
    shoot_type: str = "other",
    status: str = "lead",
    event_date: str = "",
    notes: str = "",
) -> dict:
    if status not in PROJECT_STATUSES:
        status = "lead"
    cur = conn.execute(
        """
        INSERT INTO projects (tenant_id, client_id, name, shoot_type, status, event_date, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, name.strip(), normalize_shoot_type(shoot_type), status,
         event_date.strip() or None, notes.strip()),
    )
    return get_project(conn, tenant_id, cur.lastrowid)


def get_project(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT p.*, c.name AS client_name
          FROM projects p
          LEFT JOIN clients c ON c.id = p.client_id
         WHERE p.id = ? AND p.tenant_id = ?
        """,
        (project_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def list_projects(conn: sqlite3.Connection, tenant_id: str, *, client_id: int | None = None) -> list[dict]:
    sql = (
        "SELECT p.*, c.name AS client_name, "
        "       (SELECT COUNT(*) FROM galleries g WHERE g.project_id = p.id) AS gallery_count "
        "  FROM projects p LEFT JOIN clients c ON c.id = p.client_id "
        " WHERE p.tenant_id = ?"
    )
    params: list = [tenant_id]
    if client_id is not None:
        sql += " AND p.client_id = ?"
        params.append(client_id)
    sql += " ORDER BY p.created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_project_status(conn: sqlite3.Connection, tenant_id: str, project_id: int, status: str) -> None:
    if status not in PROJECT_STATUSES:
        return
    conn.execute(
        "UPDATE projects SET status = ? WHERE id = ? AND tenant_id = ?",
        (status, project_id, tenant_id),
    )
    if status == "booked":
        emit_event(conn, tenant_id=tenant_id, event="project.booked",
                   context={"project_id": project_id})
        # A referred lead that books pays its referrer a credit (idempotent). Lazy
        # import keeps crm ⇄ referral_rewards from forming an import cycle.
        from .referral_rewards import award_referral_credit
        award_referral_credit(conn, tenant_id, project_id)


# ── Gallery ↔ project linkage ───────────────────────────────────────────────


def assign_gallery_to_project(
    conn: sqlite3.Connection, tenant_id: str, gallery_id: int, project_id: int | None
) -> None:
    # Validate the project belongs to the tenant (or clear the link).
    if project_id is not None and not get_project(conn, tenant_id, project_id):
        return
    conn.execute(
        "UPDATE galleries SET project_id = ? WHERE id = ? AND tenant_id = ?",
        (project_id, gallery_id, tenant_id),
    )


def galleries_for_project(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM galleries WHERE tenant_id = ? AND project_id = ? ORDER BY created_at DESC",
        (tenant_id, project_id),
    ).fetchall()
    return [dict(r) for r in rows]


def galleries_for_client(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> list[dict]:
    """Every gallery whose project belongs to this client (for the client portal)."""
    rows = conn.execute(
        "SELECT g.* FROM galleries g JOIN projects p ON p.id = g.project_id "
        "WHERE g.tenant_id = ? AND p.client_id = ? ORDER BY g.created_at DESC",
        (tenant_id, client_id),
    ).fetchall()
    return [dict(r) for r in rows]

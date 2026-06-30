"""Tenant ownership checks for optional parent links."""

from __future__ import annotations

import sqlite3


def owned_client_id(conn: sqlite3.Connection, tenant_id: str, client_id: int | None) -> int | None:
    """Return the client id only if it belongs to the tenant."""
    if client_id is None:
        return None
    row = conn.execute(
        "SELECT 1 FROM clients WHERE id = ? AND tenant_id = ?",
        (client_id, tenant_id),
    ).fetchone()
    return client_id if row else None


def owned_project_id(conn: sqlite3.Connection, tenant_id: str, project_id: int | None) -> int | None:
    """Return the project id only if it belongs to the tenant."""
    if project_id is None:
        return None
    row = conn.execute(
        "SELECT 1 FROM projects WHERE id = ? AND tenant_id = ?",
        (project_id, tenant_id),
    ).fetchone()
    return project_id if row else None


def normalize_client_project_ids(
    conn: sqlite3.Connection,
    tenant_id: str,
    client_id: int | None,
    project_id: int | None,
) -> tuple[int | None, int | None]:
    """Return tenant-owned parent ids, clearing a project that does not belong to the client."""
    client_id = owned_client_id(conn, tenant_id, client_id)
    project_id = owned_project_id(conn, tenant_id, project_id)
    if client_id is None or project_id is None:
        return client_id, project_id
    row = conn.execute(
        "SELECT client_id FROM projects WHERE id = ? AND tenant_id = ?",
        (project_id, tenant_id),
    ).fetchone()
    if not row or row["client_id"] != client_id:
        project_id = None
    return client_id, project_id


def owned_gallery_id(conn: sqlite3.Connection, tenant_id: str, gallery_id: int | None) -> int | None:
    """Return the gallery id only if it belongs to the tenant."""
    if gallery_id is None:
        return None
    row = conn.execute(
        "SELECT 1 FROM galleries WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    return gallery_id if row else None

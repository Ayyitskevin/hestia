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

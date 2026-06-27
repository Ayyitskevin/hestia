"""Project checklist — a simple per-project task list so a studio never drops a
deliverable. Tasks belong to a project, toggle done/undone, and roll up to a
progress count for the project page. Tenant-scoped throughout (every read and
write is gated on ``tenant_id``)."""

from __future__ import annotations

import sqlite3


def add_task(conn: sqlite3.Connection, *, tenant_id: str, project_id: int, label: str) -> dict | None:
    """Add a checklist item to a project. Empty/whitespace labels are ignored."""
    text = (label or "").strip()
    if not text:
        return None
    cur = conn.execute(
        "INSERT INTO project_tasks (tenant_id, project_id, label) VALUES (?, ?, ?)",
        (tenant_id, project_id, text[:200]),
    )
    return get_task(conn, tenant_id, cur.lastrowid)


def get_task(conn: sqlite3.Connection, tenant_id: str, task_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM project_tasks WHERE id = ? AND tenant_id = ?", (task_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_tasks(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> list[dict]:
    """Tasks for a project — open ones first, then completed, stable by creation."""
    rows = conn.execute(
        "SELECT * FROM project_tasks WHERE tenant_id = ? AND project_id = ? ORDER BY done, id",
        (tenant_id, project_id),
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_task(conn: sqlite3.Connection, tenant_id: str, task_id: int) -> None:
    """Flip a task between done and not-done (tenant-scoped no-op if not found)."""
    conn.execute(
        "UPDATE project_tasks SET done = 1 - done, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (task_id, tenant_id),
    )


def delete_task(conn: sqlite3.Connection, tenant_id: str, task_id: int) -> None:
    conn.execute("DELETE FROM project_tasks WHERE id = ? AND tenant_id = ?", (task_id, tenant_id))


def task_progress(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> dict:
    """Done/total/percent for a project's checklist — drives the progress label."""
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(done), 0) AS done "
        "FROM project_tasks WHERE tenant_id = ? AND project_id = ?",
        (tenant_id, project_id),
    ).fetchone()
    total, done = int(row["total"]), int(row["done"])
    return {"total": total, "done": done, "pct": round(100 * done / total) if total else 0}

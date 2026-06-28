"""Project checklist templates — a studio's reusable deliverable lists.

A studio defines its go-to checklist once per shoot type (or ``'any'`` for all), and
:func:`apply_checklist` copies the matching items onto a project's task list — when the
project books (automatically) or on demand. It *copies* rather than links, so editing a
template never rewrites past projects, and re-applying is idempotent: an item already on
the project (matched by label) is skipped, so a re-book or a double-click adds nothing.
Everything is tenant-scoped.
"""

from __future__ import annotations

import sqlite3

from .features import normalize_shoot_type
from .project_tasks import add_task

# the pseudo shoot type that applies to every project regardless of its type
ANY = "any"


def add_template_task(conn: sqlite3.Connection, *, tenant_id: str, shoot_type: str,
                      label: str) -> dict | None:
    """Add a template checklist item for a shoot type (or ``'any'``). Blank labels ignored."""
    text = (label or "").strip()
    if not text:
        return None
    st = ANY if (shoot_type or "").strip().lower() == ANY else normalize_shoot_type(shoot_type)
    row = conn.execute(
        "SELECT COALESCE(MAX(position), 0) AS m FROM task_templates "
        "WHERE tenant_id = ? AND shoot_type = ?",
        (tenant_id, st),
    ).fetchone()
    pos = (row["m"] if row else 0) + 1
    cur = conn.execute(
        "INSERT INTO task_templates (tenant_id, shoot_type, label, position) VALUES (?, ?, ?, ?)",
        (tenant_id, st, text[:200], pos),
    )
    return get_template_task(conn, tenant_id, cur.lastrowid)


def get_template_task(conn: sqlite3.Connection, tenant_id: str, template_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM task_templates WHERE id = ? AND tenant_id = ?", (template_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_template_tasks(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """All of a tenant's template items, grouped sensibly (by shoot type, then position)."""
    rows = conn.execute(
        "SELECT * FROM task_templates WHERE tenant_id = ? ORDER BY shoot_type, position, id",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_template_task(conn: sqlite3.Connection, tenant_id: str, template_id: int) -> None:
    conn.execute("DELETE FROM task_templates WHERE id = ? AND tenant_id = ?",
                 (template_id, tenant_id))


def apply_checklist(conn: sqlite3.Connection, tenant_id: str, project_id: int) -> int:
    """Copy the template items matching a project's shoot type (plus the ``'any'`` items)
    onto its checklist. Idempotent: an item already present on the project (by label) is
    skipped, so booking, re-booking, or clicking apply twice never duplicates a task.
    Returns the number of tasks added."""
    proj = conn.execute(
        "SELECT shoot_type FROM projects WHERE id = ? AND tenant_id = ?", (project_id, tenant_id)
    ).fetchone()
    if not proj:
        return 0
    templates = conn.execute(
        "SELECT label FROM task_templates WHERE tenant_id = ? AND shoot_type IN (?, ?) "
        "ORDER BY shoot_type, position, id",
        (tenant_id, proj["shoot_type"], ANY),
    ).fetchall()
    existing = {
        r["label"] for r in conn.execute(
            "SELECT label FROM project_tasks WHERE tenant_id = ? AND project_id = ?",
            (tenant_id, project_id))
    }
    added = 0
    for t in templates:
        if t["label"] not in existing:
            add_task(conn, tenant_id=tenant_id, project_id=project_id, label=t["label"])
            existing.add(t["label"])
            added += 1
    return added

"""Questionnaire data access — client intake forms with an idempotent submit.

Statuses: ``draft → sent → completed`` (or ``void``). A questionnaire is a title
plus an ordered list of prompts; the client's answers are written back onto the
prompt rows on submit. Submitting is idempotent — the ``sent → completed``
transition happens exactly once (guarded by ``WHERE status = 'sent'``), so a
double submit or a re-opened link never overwrites the captured answers.
Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from .automations import emit_event
from .crypto import new_session_token
from .db import audit

QUESTIONNAIRE_STATUSES = ("draft", "sent", "completed", "void")


def create_questionnaire(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    title: str,
    prompts: list[str],
    client_id: int | None = None,
    project_id: int | None = None,
) -> dict:
    token = new_session_token()[:28]
    cur = conn.execute(
        "INSERT INTO questionnaires (tenant_id, client_id, project_id, title, token) "
        "VALUES (?, ?, ?, ?, ?)",
        (tenant_id, client_id, project_id, title.strip(), token),
    )
    qid = cur.lastrowid
    clean = [p.strip() for p in prompts if p.strip()]
    for seq, prompt in enumerate(clean, start=1):
        conn.execute(
            "INSERT INTO questionnaire_items (questionnaire_id, tenant_id, sequence, prompt) "
            "VALUES (?, ?, ?, ?)",
            (qid, tenant_id, seq, prompt),
        )
    return get_questionnaire(conn, tenant_id, qid)


def _items(conn: sqlite3.Connection, tenant_id: str, qid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM questionnaire_items WHERE tenant_id = ? AND questionnaire_id = ? "
        "ORDER BY sequence, id",
        (tenant_id, qid),
    ).fetchall()
    return [dict(r) for r in rows]


def get_questionnaire(conn: sqlite3.Connection, tenant_id: str, qid: int) -> dict | None:
    row = conn.execute(
        """
        SELECT q.*, c.name AS client_name, c.email AS client_email, p.name AS project_name
          FROM questionnaires q
          LEFT JOIN clients c ON c.id = q.client_id
          LEFT JOIN projects p ON p.id = q.project_id
         WHERE q.id = ? AND q.tenant_id = ?
        """,
        (qid, tenant_id),
    ).fetchone()
    if not row:
        return None
    q = dict(row)
    q["items"] = _items(conn, tenant_id, qid)
    return q


def get_questionnaire_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        """
        SELECT q.*, c.name AS client_name, p.name AS project_name
          FROM questionnaires q
          LEFT JOIN clients c ON c.id = q.client_id
          LEFT JOIN projects p ON p.id = q.project_id
         WHERE q.token = ?
        """,
        (token,),
    ).fetchone()
    if not row:
        return None
    q = dict(row)
    q["items"] = _items(conn, q["tenant_id"], q["id"])
    return q


def list_questionnaires(
    conn: sqlite3.Connection, tenant_id: str, *,
    project_id: int | None = None, client_id: int | None = None,
) -> list[dict]:
    sql = (
        "SELECT q.*, c.name AS client_name, p.name AS project_name, "
        "       (SELECT COUNT(*) FROM questionnaire_items qi WHERE qi.questionnaire_id = q.id) AS item_count "
        "  FROM questionnaires q "
        "  LEFT JOIN clients c ON c.id = q.client_id "
        "  LEFT JOIN projects p ON p.id = q.project_id "
        " WHERE q.tenant_id = ?"
    )
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND q.project_id = ?"
        params.append(project_id)
    if client_id is not None:
        sql += " AND q.client_id = ?"
        params.append(client_id)
    sql += " ORDER BY q.created_at DESC"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def send_questionnaire(conn: sqlite3.Connection, tenant_id: str, qid: int) -> None:
    """Make a questionnaire fillable (draft→sent). Re-sending while sent re-emails
    the link; a completed or void questionnaire is untouched."""
    conn.execute(
        "UPDATE questionnaires SET status = 'sent', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status IN ('draft', 'sent')",
        (qid, tenant_id),
    )


def void_questionnaire(conn: sqlite3.Connection, tenant_id: str, qid: int) -> None:
    """Void a questionnaire. A completed one stays — its answers are kept."""
    conn.execute(
        "UPDATE questionnaires SET status = 'void', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status != 'completed'",
        (qid, tenant_id),
    )


def submit_questionnaire(conn: sqlite3.Connection, *, token: str, answers: dict) -> bool:
    """Idempotently record a client's answers. Returns True only on the single
    ``sent → completed`` transition; later submits are no-ops (return False).

    The status flips first under a ``WHERE status = 'sent'`` guard, so only the
    first submit wins the transition and writes the answers — a re-opened link
    can't overwrite them.
    """
    cur = conn.execute(
        "UPDATE questionnaires SET status = 'completed', updated_at = datetime('now') "
        "WHERE token = ? AND status = 'sent'",
        (token,),
    )
    if cur.rowcount == 0:
        return False
    q = conn.execute(
        "SELECT id, tenant_id, title, client_id, project_id FROM questionnaires WHERE token = ?",
        (token,),
    ).fetchone()
    for item in _items(conn, q["tenant_id"], q["id"]):
        ans = (answers.get(str(item["id"])) or "").strip()
        conn.execute(
            "UPDATE questionnaire_items SET answer = ?, answered_at = datetime('now') "
            "WHERE id = ? AND tenant_id = ?",
            (ans, item["id"], q["tenant_id"]),
        )
    audit(conn, actor="client", action="questionnaire.completed", tenant_id=q["tenant_id"],
          detail=q["title"])
    emit_event(conn, tenant_id=q["tenant_id"], event="questionnaire.completed",
               context={"client_id": q["client_id"], "project_id": q["project_id"],
                        "title": q["title"]})
    return True

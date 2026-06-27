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

from . import messaging
from .automations import emit_event
from .config import Settings
from .crypto import new_session_token
from .db import audit
from .email import notify

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
          LEFT JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id
          LEFT JOIN projects p ON p.id = q.project_id AND p.tenant_id = q.tenant_id
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
          LEFT JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id
          LEFT JOIN projects p ON p.id = q.project_id AND p.tenant_id = q.tenant_id
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
        "  LEFT JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id "
        "  LEFT JOIN projects p ON p.id = q.project_id AND p.tenant_id = q.tenant_id "
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


def questionnaire_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/q/{token}"


# --- chase responses: nudge clients sitting on an unfilled questionnaire --------


def send_questionnaire_reminder(conn: sqlite3.Connection, settings: Settings, q: dict) -> str | None:
    """Email a friendly nudge with the fill-out link. Returns the send status, or
    None when the client has no email on file."""
    to = (q.get("client_email") or "").strip()
    if not to:
        return None
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (q["tenant_id"],)).fetchone()
    ctx = {
        "client": q.get("client_name") or "there",
        "studio": trow["name"] if trow else "your photographer",
        "title": q["title"], "fill_url": questionnaire_public_url(settings, q["token"]),
    }
    msg = messaging.render(conn, q["tenant_id"], "questionnaire_reminder", ctx)
    return notify(conn, settings, to=to, subject=msg["subject"], body=msg["body"],
                  tenant_id=q["tenant_id"])


def record_questionnaire_reminder(conn: sqlite3.Connection, tenant_id: str, qid: int) -> bool:
    """Atomically stamp a reminder as sent — gates the next nudge. Only a still-'sent'
    questionnaire is stamped; True iff a row changed (claim-before-send)."""
    cur = conn.execute(
        "UPDATE questionnaires SET last_reminder_at = datetime('now'), "
        "reminder_count = reminder_count + 1 WHERE id = ? AND tenant_id = ? AND status = 'sent'",
        (qid, tenant_id),
    )
    return cur.rowcount > 0


def send_incomplete_reminders(conn: sqlite3.Connection, settings: Settings, *,
                              cooldown_days: int = 7, limit: int = 500) -> int:
    """Across all tenants, nudge each unfilled ('sent') questionnaire whose client has
    an email and hasn't been reminded within the cooldown. Each is claimed first (an
    atomic UPDATE gated on status='sent'); only a successful claim sends, so a
    questionnaire completed between this SELECT and the send gets no late nudge."""
    rows = conn.execute(
        "SELECT q.id, q.tenant_id, q.title, q.token, c.name AS client_name, c.email AS client_email "
        "FROM questionnaires q JOIN clients c ON c.id = q.client_id AND c.tenant_id = q.tenant_id "
        "WHERE q.status = 'sent' AND TRIM(COALESCE(c.email, '')) <> '' "
        "  AND (q.last_reminder_at IS NULL OR q.last_reminder_at < datetime('now', ?)) "
        "ORDER BY q.id LIMIT ?",
        (f"-{int(cooldown_days)} days", limit),
    ).fetchall()
    sent = 0
    for r in rows:
        q = dict(r)
        if record_questionnaire_reminder(conn, q["tenant_id"], q["id"]):   # claim before send
            send_questionnaire_reminder(conn, settings, q)
            sent += 1
    return sent


# --- reusable question-set templates: save an intake, start a questionnaire from it ---


def _clean_prompts(prompts: str) -> str:
    """Normalize a prompts blob to one trimmed, non-blank question per line."""
    return "\n".join(ln.strip() for ln in (prompts or "").splitlines() if ln.strip())


def save_questionnaire_template(conn: sqlite3.Connection, *, tenant_id: str, name: str,
                                prompts: str) -> dict | None:
    """Save a named reusable question set. Empty name is ignored (returns None); the
    prompts are normalized to one question per line (same format as the create form)."""
    label = (name or "").strip()
    if not label:
        return None
    cur = conn.execute(
        "INSERT INTO questionnaire_templates (tenant_id, name, prompts) VALUES (?, ?, ?)",
        (tenant_id, label[:200], _clean_prompts(prompts)),
    )
    return get_questionnaire_template(conn, tenant_id, cur.lastrowid)


def list_questionnaire_templates(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM questionnaire_templates WHERE tenant_id = ? ORDER BY name, id", (tenant_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["prompt_count"] = len([ln for ln in d["prompts"].splitlines() if ln.strip()])
        out.append(d)
    return out


def get_questionnaire_template(conn: sqlite3.Connection, tenant_id: str,
                               template_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM questionnaire_templates WHERE id = ? AND tenant_id = ?",
        (template_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def delete_questionnaire_template(conn: sqlite3.Connection, tenant_id: str,
                                  template_id: int) -> None:
    conn.execute(
        "DELETE FROM questionnaire_templates WHERE id = ? AND tenant_id = ?",
        (template_id, tenant_id),
    )

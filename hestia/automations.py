"""Workflow engine — event-triggered automations ("when X, email the client").

Two halves, deliberately decoupled:

- **Emission** (:func:`emit_event`) runs inside the triggering transaction with
  only a connection. It finds the tenant's enabled rules for the event and
  enqueues one durable job each — cheap, and it commits atomically with the thing
  that triggered it (a signed contract and its follow-up email succeed together).
- **Execution** (the ``automation.run`` job handler) runs on the worker with full
  settings: it renders the rule's template against the event context and sends
  via the email seam, recording every outcome in ``automation_runs``.

The first action is ``email_client``; the model leaves room for more. Templates
support ``{client_name}``, ``{studio_name}``, ``{project_name}``, ``{title}``.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .email import notify
from .jobs import enqueue, register

# The events a rule can trigger on — value is the human label for the UI.
TRIGGERS: dict[str, str] = {
    "contract.signed": "Contract signed",
    "invoice.paid": "Invoice paid",
    "questionnaire.completed": "Questionnaire completed",
    "project.booked": "Project marked booked",
    "gallery.published": "Gallery published",
    "appointment.confirmed": "Appointment confirmed",
}

ACTIONS: dict[str, str] = {
    "email_client": "Email the client",
}

PLACEHOLDERS = ("client_name", "studio_name", "project_name", "title")


def emit_event(
    conn: sqlite3.Connection, *, tenant_id: str, event: str, context: dict | None = None
) -> int:
    """Enqueue a job for each enabled rule matching ``event``. Connection-only, so
    it runs inside the triggering transaction. Returns the number of jobs queued."""
    rows = conn.execute(
        "SELECT id FROM automations WHERE tenant_id = ? AND trigger = ? AND enabled = 1",
        (tenant_id, event),
    ).fetchall()
    for r in rows:
        enqueue(conn, kind="automation.run", tenant_id=tenant_id,
                payload={"automation_id": r["id"], "event": event, "context": context or {}})
    return len(rows)


def _render(template: str, fields: dict) -> str:
    out = template
    for key in PLACEHOLDERS:
        out = out.replace("{" + key + "}", str(fields.get(key, "")))
    return out


@register("automation.run")
def _run_automation(settings: Settings, payload: dict) -> None:
    """Job handler: render one rule against the event context and send it."""
    from .db import get_db

    automation_id = int(payload["automation_id"])
    ctx = payload.get("context", {})
    with get_db(settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM automations WHERE id = ?", (automation_id,)
        ).fetchone()
        # Rule may have been disabled or deleted between emit and run — that's fine.
        if not row or not row["enabled"]:
            return
        auto = dict(row)
        status, detail = _execute(conn, settings, auto, ctx)
        conn.execute(
            "INSERT INTO automation_runs (tenant_id, automation_id, trigger, status, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (auto["tenant_id"], auto["id"], auto["trigger"], status, detail),
        )


def _execute(conn: sqlite3.Connection, settings: Settings, auto: dict, ctx: dict) -> tuple[str, str]:
    from .crm import get_client, get_project
    from .tenants import get_tenant

    tenant_id = auto["tenant_id"]
    project = None
    if ctx.get("project_id"):
        project = get_project(conn, tenant_id, int(ctx["project_id"]))
    client_id = ctx.get("client_id") or (project or {}).get("client_id")
    client = get_client(conn, tenant_id, int(client_id)) if client_id else None
    tenant = get_tenant(conn, tenant_id)
    project_name = ctx.get("project_name") or (project or {}).get("name", "")
    fields = {
        "client_name": (client or {}).get("name") or "there",
        "studio_name": (tenant or {}).get("name", ""),
        "project_name": project_name,
        "title": ctx.get("title") or project_name,
    }
    subject = _render(auto["subject"], fields)
    body = _render(auto["body"], fields)

    # Only action for now: email the client. No recipient → record a skip, not a failure.
    to = (client or {}).get("email", "")
    if not to:
        return "skipped", "no client email on file"
    notify(conn, settings, to=to, subject=subject, body=body, tenant_id=tenant_id)
    return "sent", f"emailed {to}"


# ── Rule CRUD ────────────────────────────────────────────────────────────────


def create_automation(
    conn: sqlite3.Connection, *, tenant_id: str, name: str, trigger: str,
    subject: str, body: str, action: str = "email_client",
) -> dict | None:
    if trigger not in TRIGGERS or action not in ACTIONS:
        return None
    cur = conn.execute(
        "INSERT INTO automations (tenant_id, name, trigger, action, subject, body) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, name.strip(), trigger, action, subject.strip(), body.strip()),
    )
    return get_automation(conn, tenant_id, cur.lastrowid)


def get_automation(conn: sqlite3.Connection, tenant_id: str, automation_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM automations WHERE id = ? AND tenant_id = ?", (automation_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_automations(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM automations WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
    ).fetchall()
    out = []
    for r in rows:
        a = dict(r)
        a["trigger_label"] = TRIGGERS.get(a["trigger"], a["trigger"])
        out.append(a)
    return out


def set_automation_enabled(
    conn: sqlite3.Connection, tenant_id: str, automation_id: int, enabled: bool
) -> None:
    conn.execute(
        "UPDATE automations SET enabled = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (1 if enabled else 0, automation_id, tenant_id),
    )


def delete_automation(conn: sqlite3.Connection, tenant_id: str, automation_id: int) -> None:
    conn.execute(
        "DELETE FROM automations WHERE id = ? AND tenant_id = ?", (automation_id, tenant_id)
    )


def list_runs(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT r.*, a.name AS automation_name FROM automation_runs r "
        "LEFT JOIN automations a ON a.id = r.automation_id "
        "WHERE r.tenant_id = ? ORDER BY r.id DESC LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]

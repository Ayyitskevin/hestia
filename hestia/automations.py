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
support ``{client_name}``, ``{studio_name}``, ``{project_name}``, ``{title}``,
and the event-specific ``{gallery_url}``.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .email import delivery_ok, notify
from .jobs import NonRetryableJobError, enqueue, register

# The events a rule can trigger on — value is the human label for the UI.
TRIGGERS: dict[str, str] = {
    "contract.signed": "Contract signed",
    "invoice.paid": "Invoice paid",
    "questionnaire.completed": "Questionnaire completed",
    "project.booked": "Project marked booked",
    "gallery.published": "Gallery published",
    "gallery.selections_submitted": "Client submitted gallery selections",
    "album.approved": "Client approved album",
    "album.changes_requested": "Client requested album changes",
    "appointment.confirmed": "Appointment confirmed",
    "appointment.canceled": "Appointment canceled",
    "booking.requested": "Booking requested (awaiting confirmation)",
}

ACTIONS: dict[str, str] = {
    "email_client": "Email the client",
}

PLACEHOLDERS = ("client_name", "studio_name", "project_name", "title", "gallery_url")


def emit_event(
    conn: sqlite3.Connection, *, tenant_id: str, event: str, context: dict | None = None
) -> int:
    """Enqueue a job for each enabled rule matching ``event``. Connection-only, so
    it runs inside the triggering transaction. A rule with ``delay_days > 0`` is
    scheduled that many days out (retention); ``0`` fires as soon as a worker is
    free. Returns the number of jobs queued."""
    rows = conn.execute(
        "SELECT id, delay_days FROM automations WHERE tenant_id = ? AND trigger = ? AND enabled = 1",
        (tenant_id, event),
    ).fetchall()
    for r in rows:
        run_at = None
        if r["delay_days"] and int(r["delay_days"]) > 0:
            run_at = conn.execute(
                "SELECT datetime('now', ?)", (f"+{int(r['delay_days'])} days",)
            ).fetchone()[0]
        enqueue(conn, kind="automation.run", tenant_id=tenant_id, run_at=run_at,
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
    if not isinstance(ctx, dict):
        ctx = {}
    with get_db(settings.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM automations WHERE id = ?", (automation_id,)
        ).fetchone()
        # Rule may have been disabled or deleted between emit and run — that's fine.
        if not row or not row["enabled"]:
            return
        if not settings.automation_email_enabled:
            raise RuntimeError("automation email delivery pause was bypassed")
        auto = dict(row)
        status, detail = _execute(conn, settings, auto, ctx)
        conn.execute(
            "INSERT INTO automation_runs (tenant_id, automation_id, trigger, status, detail) "
            "VALUES (?, ?, ?, ?, ?)",
            (auto["tenant_id"], auto["id"], auto["trigger"], status, detail),
        )
        if status == "failed":
            # Preserve the generic failed run, then dead-letter the job. SMTP can fail
            # after accepting a message but before acknowledging it, so blind replay can
            # duplicate client mail. An operator must review this ambiguous outcome.
            conn.commit()
            raise NonRetryableJobError(detail)


def _execute(conn: sqlite3.Connection, settings: Settings, auto: dict, ctx: dict) -> tuple[str, str]:
    from .crm import get_client, get_project
    from .galleries import gallery_proofing_url, get_gallery
    from .tenants import get_tenant

    tenant_id = auto["tenant_id"]
    tenant = get_tenant(conn, tenant_id)
    gallery_url = ""
    gallery = None
    needs_gallery_url = any(
        "{gallery_url}" in (auto.get(field) or "") for field in ("subject", "body")
    )
    is_gallery_publish = auto["trigger"] == "gallery.published"
    if needs_gallery_url and not is_gallery_publish:
        return "skipped", "gallery_url unavailable for automation trigger"
    legacy_gallery_publish = is_gallery_publish and "gallery_id" not in ctx
    if legacy_gallery_publish and needs_gallery_url:
        return "skipped", "queued gallery authority is missing"
    has_gallery_context = is_gallery_publish and not legacy_gallery_publish
    if has_gallery_context:
        raw_gallery_id = ctx.get("gallery_id")
        if isinstance(raw_gallery_id, bool):
            gallery_id = None
        elif isinstance(raw_gallery_id, int):
            gallery_id = raw_gallery_id
        elif (
            isinstance(raw_gallery_id, str)
            and 1 <= len(raw_gallery_id) <= 19
            and raw_gallery_id.isascii()
            and raw_gallery_id.isdecimal()
        ):
            gallery_id = int(raw_gallery_id)
        else:
            gallery_id = None
        if not gallery_id or not 0 < gallery_id <= (1 << 63) - 1:
            return "skipped", "gallery unavailable for automation"
        gallery = get_gallery(conn, tenant_id, gallery_id)
        if not gallery or gallery["status"] != "published" or not tenant:
            return "skipped", "gallery unavailable for automation"
        gallery_url = gallery_proofing_url(settings, tenant["slug"], gallery["slug"])

    project_id = gallery.get("project_id") if gallery else ctx.get("project_id")
    project = get_project(conn, tenant_id, int(project_id)) if project_id else None
    if is_gallery_publish:
        # New jobs follow the current gallery relationship. Legacy pre-upgrade jobs
        # have only project authority, so they still re-resolve that project's current
        # client and never trust a queued client snapshot.
        client_id = (project or {}).get("client_id")
    else:
        client_id = ctx.get("client_id") or (project or {}).get("client_id")
    client = get_client(conn, tenant_id, int(client_id)) if client_id else None
    project_name = ctx.get("project_name") or (project or {}).get("name", "")
    fields = {
        "client_name": (client or {}).get("name") or "there",
        "studio_name": (tenant or {}).get("name", ""),
        "project_name": project_name,
        "title": ctx.get("title") or project_name,
        "gallery_url": gallery_url,
    }
    if needs_gallery_url and not gallery_url:
        return "skipped", "gallery unavailable for gallery_url placeholder"
    subject = _render(auto["subject"], fields)
    body = _render(auto["body"], fields)

    # Only action for now: email the client. No recipient → record a skip, not a failure.
    to = (client or {}).get("email", "")
    if not to:
        return "skipped", "no client email on file"
    email_status = notify(
        conn,
        settings,
        to=to,
        subject=subject,
        body=body,
        tenant_id=tenant_id,
    )
    if not delivery_ok(email_status):
        return "failed", "email delivery failed; manual review required"
    return "sent", f"emailed {to}"


# ── Rule CRUD ────────────────────────────────────────────────────────────────


def create_automation(
    conn: sqlite3.Connection, *, tenant_id: str, name: str, trigger: str,
    subject: str, body: str, action: str = "email_client", delay_days: int = 0,
) -> dict | None:
    if trigger not in TRIGGERS or action not in ACTIONS:
        return None
    uses_gallery_url = "{gallery_url}" in subject or "{gallery_url}" in body
    if uses_gallery_url and trigger != "gallery.published":
        return None
    cur = conn.execute(
        "INSERT INTO automations (tenant_id, name, trigger, action, subject, body, delay_days) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, name.strip(), trigger, action, subject.strip(), body.strip(),
         max(0, int(delay_days))),
    )
    return get_automation(conn, tenant_id, cur.lastrowid)


# One-click retention recipes: the timed re-engagement flows a studio should have.
RETENTION_RECIPES = {
    "review": {
        "name": "Review request", "trigger": "invoice.paid", "delay_days": 3,
        "subject": "How did we do, {client_name}?",
        "body": ("Hi {client_name},\n\nThank you for your order with {studio_name}! If you have a "
                 "moment, we'd be so grateful for a quick review of your experience.\n\n"
                 "Warmly,\n{studio_name}"),
    },
    "rebook": {
        "name": "Anniversary re-book", "trigger": "gallery.published", "delay_days": 365,
        "subject": "It's been a year, {client_name}!",
        "body": ("Hi {client_name},\n\nHard to believe it's been a year since {project_name}! If "
                 "you're ready for new photos, we'd love to work with you again — just reply to "
                 "book.\n\nWarmly,\n{studio_name}"),
    },
    "welcome": {
        "name": "Post-booking welcome", "trigger": "project.booked", "delay_days": 1,
        "subject": "Welcome to {studio_name}, {client_name}!",
        "body": ("Hi {client_name},\n\nWe're so excited for {project_name}. Here's what happens "
                 "next, and how to reach us any time.\n\nWarmly,\n{studio_name}"),
    },
    "prep": {
        "name": "Session prep guide", "trigger": "appointment.confirmed", "delay_days": 0,
        "subject": "Getting ready for {title}, {client_name}",
        "body": ("Hi {client_name},\n\nYour {title} with {studio_name} is confirmed — we can't "
                 "wait! Here are a few tips to help you prepare and make the most of our time "
                 "together. Any questions before then? Just reply.\n\nWarmly,\n{studio_name}"),
    },
    "winback": {
        "name": "Win-back after a cancellation", "trigger": "appointment.canceled", "delay_days": 2,
        "subject": "We'd still love to work with you, {client_name}",
        "body": ("Hi {client_name},\n\nWe're sorry {title} didn't work out this time. If your "
                 "plans change, we'd love to find a new time that suits you — just reply and "
                 "we'll get you back on the calendar.\n\nWarmly,\n{studio_name}"),
    },
    "booking_followup": {
        "name": "Booking-request follow-up", "trigger": "booking.requested", "delay_days": 1,
        "subject": "Following up on your {title} request, {client_name}",
        "body": ("Hi {client_name},\n\nThanks again for your interest in a {title} with "
                 "{studio_name}! We're finalizing the details and will confirm your time soon. "
                 "In the meantime, reply with any questions — we'd love to tell you more about "
                 "what to expect.\n\nWarmly,\n{studio_name}"),
    },
}


def create_from_recipe(conn: sqlite3.Connection, tenant_id: str, key: str) -> dict | None:
    r = RETENTION_RECIPES.get(key)
    if not r:
        return None
    return create_automation(
        conn, tenant_id=tenant_id, name=r["name"], trigger=r["trigger"],
        subject=r["subject"], body=r["body"], delay_days=r["delay_days"],
    )


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

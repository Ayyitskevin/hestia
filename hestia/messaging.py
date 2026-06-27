"""Customizable transactional email templates.

Each studio can override the subject and body of the client emails Hestia sends on
its behalf — booking confirmations, reminders, invoice notices. A template is a
subject + body carrying ``{variable}`` placeholders; an unset template falls back to
the built-in default, so a studio that never touches this sees no change. Rendering
substitutes the known variables and leaves any unknown ``{token}`` untouched (so a
typo can't crash a send). Emails are plain text, and the studio's signature is
appended separately by the mailer, so a template holds the message body only.
"""

from __future__ import annotations

import re
import sqlite3

# kind -> default template + the variables it may use (the editor shows these as hints).
TEMPLATES: dict[str, dict] = {
    "appointment_confirm": {
        "label": "Session confirmed",
        "subject": "Confirmed: {title} on {when}",
        "body": ("Hi {client},\n\nYour {title} with {studio} is confirmed on {when}.{location}\n\n"
                 "Add to your calendar: {calendar_url}\n\nSee you then!"),
        "variables": ["client", "studio", "title", "when", "location", "calendar_url"],
    },
    "appointment_reminder": {
        "label": "Session reminder",
        "subject": "Reminder: {title} on {when}",
        "body": ("Hi {client},\n\nA friendly reminder that your {title} with {studio} is coming up "
                 "on {when}.{location}\n\nAdd to your calendar: {calendar_url}\n\nSee you then!"),
        "variables": ["client", "studio", "title", "when", "location", "calendar_url"],
    },
    "invoice_send": {
        "label": "Invoice",
        "subject": "{studio}: invoice for {title} ({amount})",
        "body": ("Hi {client},\n\n{studio} sent you an invoice for {title} — {amount}.\n\n{note}"
                 "Pay securely here:\n{pay_url}\n\nThank you!"),
        "variables": ["client", "studio", "title", "amount", "pay_url", "note"],
    },
}

_VAR = re.compile(r"\{(\w+)\}")


def _fill(text: str, context: dict) -> str:
    """Substitute ``{var}`` from context; an unknown token is left exactly as written."""
    return _VAR.sub(lambda m: str(context.get(m.group(1), m.group(0))), text)


def get_template(conn: sqlite3.Connection, tenant_id: str, kind: str) -> dict:
    """The studio's custom subject/body for a kind, or the built-in default."""
    default = TEMPLATES[kind]
    row = conn.execute(
        "SELECT subject, body FROM message_templates WHERE tenant_id = ? AND kind = ?",
        (tenant_id, kind),
    ).fetchone()
    if row:
        return {"subject": row["subject"], "body": row["body"]}
    return {"subject": default["subject"], "body": default["body"]}


def render(conn: sqlite3.Connection, tenant_id: str, kind: str, context: dict) -> dict:
    """Resolve the template (custom or default) and fill in the variables. Returns
    ``{"subject": ..., "body": ...}``."""
    tpl = get_template(conn, tenant_id, kind)
    return {"subject": _fill(tpl["subject"], context), "body": _fill(tpl["body"], context)}


def set_template(conn: sqlite3.Connection, tenant_id: str, kind: str, *,
                 subject: str, body: str) -> None:
    """Save a studio's custom template (upsert). An unknown kind is ignored; clearing
    both fields resets to the default (so the editor's 'reset' is just saving blank)."""
    if kind not in TEMPLATES:
        return
    if not subject.strip() and not body.strip():
        reset_template(conn, tenant_id, kind)
        return
    conn.execute(
        "INSERT INTO message_templates (tenant_id, kind, subject, body) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(tenant_id, kind) DO UPDATE SET subject = excluded.subject, "
        "  body = excluded.body, updated_at = datetime('now')",
        (tenant_id, kind, subject.strip()[:300], body.strip()[:4000]),
    )


def reset_template(conn: sqlite3.Connection, tenant_id: str, kind: str) -> None:
    conn.execute(
        "DELETE FROM message_templates WHERE tenant_id = ? AND kind = ?", (tenant_id, kind)
    )


def list_templates(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """Every editable template with the studio's current (custom-or-default) text and
    whether it's been customized — drives the settings editor."""
    custom = {r["kind"]: r for r in conn.execute(
        "SELECT kind, subject, body FROM message_templates WHERE tenant_id = ?", (tenant_id,))}
    out = []
    for kind, d in TEMPLATES.items():
        c = custom.get(kind)
        out.append({
            "kind": kind, "label": d["label"], "variables": d["variables"],
            "subject": c["subject"] if c else d["subject"],
            "body": c["body"] if c else d["body"],
            "customized": c is not None,
        })
    return out

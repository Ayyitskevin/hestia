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
    "inquiry_reply": {
        "label": "Reply to an inquiry",
        "subject": "Thanks for reaching out to {studio}!",
        "body": ("Hi {client},\n\nThank you so much for your inquiry — I'd love to hear more "
                 "about what you have in mind, and I'd be glad to put together the details for "
                 "you.\n\nWhat's the best way to reach you for a quick chat?\n\nWarmly,\n{studio}"),
        "variables": ["client", "studio"],
    },
    "broadcast": {
        "label": "Announcement / broadcast",
        "subject": "A note from {studio}",
        "body": ("Hi {client},\n\nI wanted to share a quick update.\n\n"
                 "[Write your announcement here — mini-sessions, a price change, holiday "
                 "availability, anything you'd like your clients to know.]\n\nWarmly,\n{studio}"),
        "variables": ["client", "studio"],
    },
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
    "invoice_reminder": {
        "label": "Invoice reminder (not yet due)",
        "subject": 'Reminder: invoice "{title}" from {studio}',
        "body": ("Hi {client},\n\na friendly reminder about your invoice from {studio} — "
                 '"{title}" for {amount}.\n\nYou can pay securely here:\n{pay_url}\n\n'
                 "Thank you!\n{studio}"),
        "variables": ["client", "studio", "title", "amount", "pay_url"],
    },
    "invoice_overdue": {
        "label": "Invoice reminder (past due)",
        "subject": 'Reminder: invoice "{title}" is past due',
        "body": ('Hi {client},\n\nyour invoice from {studio} — "{title}" for {amount} — is now '
                 "past due.\n\nYou can pay securely here:\n{pay_url}\n\nThank you!\n{studio}"),
        "variables": ["client", "studio", "title", "amount", "pay_url"],
    },
    "invoice_receipt": {
        "label": "Payment receipt",
        "subject": "Receipt: {title} — paid",
        "body": ("Hi {client},\n\nThank you! We've received your payment of {amount} for "
                 "{title}. This is your receipt — no action needed.\n\nWith thanks,\n{studio}"),
        "variables": ["client", "studio", "title", "amount"],
    },
    "contract_send": {
        "label": "Contract to sign",
        "subject": "{studio}: please review and sign — {title}",
        "body": ("Hi {client},\n\n{studio} has sent you a contract to review and sign: {title}.\n\n"
                 "Review and sign here:\n{sign_url}\n\nThank you!"),
        "variables": ["client", "studio", "title", "sign_url"],
    },
    "contract_reminder": {
        "label": "Contract reminder",
        "subject": 'Reminder: please sign "{title}"',
        "body": ("Hi {client},\n\nA friendly reminder from {studio} to review and sign your "
                 'contract — "{title}". It only takes a minute.\n\n'
                 "Review and sign here:\n{sign_url}\n\nThank you!\n{studio}"),
        "variables": ["client", "studio", "title", "sign_url"],
    },
    "questionnaire_send": {
        "label": "Questionnaire",
        "subject": "{studio}: a quick questionnaire — {title}",
        "body": ("Hi {client},\n\n{studio} would love a few details for {title}.\n\n"
                 "Fill it out here:\n{fill_url}\n\nThank you!"),
        "variables": ["client", "studio", "title", "fill_url"],
    },
    "questionnaire_reminder": {
        "label": "Questionnaire reminder",
        "subject": 'Reminder: a quick questionnaire — "{title}"',
        "body": ("Hi {client},\n\nA friendly reminder from {studio} — we'd still love a few "
                 'details for "{title}". It only takes a minute.\n\n'
                 "Fill it out here:\n{fill_url}\n\nThank you!\n{studio}"),
        "variables": ["client", "studio", "title", "fill_url"],
    },
}

_VAR = re.compile(r"\{(\w+)\}")


def _fill(text: str, context: dict) -> str:
    """Substitute ``{var}`` from context; an unknown token is left exactly as written."""
    return _VAR.sub(lambda m: str(context.get(m.group(1), m.group(0))), text)


def fill(text: str, context: dict) -> str:
    """Public helper to substitute ``{var}`` placeholders in free-text (e.g. a broadcast
    body typed by the owner), leaving unknown tokens intact. Mirrors template rendering."""
    return _fill(text, context)


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


def _sample_context(studio: str) -> dict:
    """Plausible stand-in values for every variable, for the editor's live preview."""
    return {
        "client": "Jordan Lee", "studio": studio or "Your Studio",
        "title": "Summer Session", "when": "Sat, Jul 18 · 2:00 PM",
        "location": "\nLocation: Riverside Park", "amount": "$1,500.00", "note": "",
        "calendar_url": "https://example.com/book/abc123/calendar.ics",
        "pay_url": "https://example.com/pay/abc123",
        "sign_url": "https://example.com/sign/abc123",
        "fill_url": "https://example.com/q/abc123",
    }


def list_templates(conn: sqlite3.Connection, tenant_id: str, *, studio: str = "") -> list[dict]:
    """Every editable template with the studio's current (custom-or-default) text,
    whether it's been customized, and a sample-data preview — drives the settings
    editor. ``studio`` (the tenant's name) is used in the preview only."""
    custom = {r["kind"]: r for r in conn.execute(
        "SELECT kind, subject, body FROM message_templates WHERE tenant_id = ?", (tenant_id,))}
    sample = _sample_context(studio)
    out = []
    for kind, d in TEMPLATES.items():
        c = custom.get(kind)
        subject = c["subject"] if c else d["subject"]
        body = c["body"] if c else d["body"]
        out.append({
            "kind": kind, "label": d["label"], "variables": d["variables"],
            "subject": subject, "body": body, "customized": c is not None,
            "preview_subject": _fill(subject, sample), "preview_body": _fill(body, sample),
        })
    return out

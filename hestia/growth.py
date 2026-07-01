"""Growth flywheel — ask happy clients for reviews and referrals.

This sits on top of existing Hestia primitives instead of adding another CRM:
paid invoices and delivered galleries identify happy clients, testimonials capture
social proof, referral links attribute word-of-mouth, emails carry the ask, and
audit_log provides the cooldown.
"""

from __future__ import annotations

import sqlite3

from . import messaging
from .config import Settings
from .crm import get_client
from .db import audit
from .email import notify
from .invoices import money
from .referrals import referral_code_for, referral_link
from .tenants import get_tenant
from .testimonials import (
    latest_testimonial_for_client,
    pending_testimonial,
    request_testimonial,
    testimonial_public_url,
)

ASK_COOLDOWN_DAYS = 30


def _audit_detail(client_id: int) -> str:
    return f"client:{int(client_id)}"


def last_growth_ask_at(conn: sqlite3.Connection, tenant_id: str, client_id: int) -> str | None:
    row = conn.execute(
        "SELECT created_at FROM audit_log "
        "WHERE tenant_id = ? AND action = 'growth.ask_sent' AND detail = ? "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id, _audit_detail(client_id)),
    ).fetchone()
    return row["created_at"] if row else None


def growth_ask_recent(
    conn: sqlite3.Connection,
    tenant_id: str,
    client_id: int,
    *,
    cooldown_days: int = ASK_COOLDOWN_DAYS,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit_log "
        "WHERE tenant_id = ? AND action = 'growth.ask_sent' AND detail = ? "
        "AND created_at >= datetime('now', ?) LIMIT 1",
        (tenant_id, _audit_detail(client_id), f"-{max(0, int(cooldown_days))} days"),
    ).fetchone()
    return row is not None


def _review_state(row: dict | None) -> str:
    if not row:
        return "not_requested"
    return row["status"]


def _opportunity_status(row: dict, *, recent: bool) -> tuple[str, str, bool]:
    if not (row.get("email") or "").strip():
        return "missing_email", "Add email first", False
    if recent:
        return "cooldown", "Asked recently", False
    if row["review_state"] == "requested":
        return "pending_review", "Review link pending", True
    if row["review_state"] in ("submitted", "featured"):
        return "referral_ready", "Referral ask ready", True
    return "ready", "Review + referral ask ready", True


def growth_opportunities(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 8) -> list[dict]:
    """Clients with enough positive signal to ask for social proof or referrals."""
    rows = conn.execute(
        """
        SELECT
            *
          FROM (
            SELECT
                c.id, c.name, c.email, c.referral_code, c.created_at,
                COALESCE((SELECT SUM(i.amount_cents) FROM invoices i
                           WHERE i.tenant_id = c.tenant_id AND i.client_id = c.id
                             AND i.status = 'paid'), 0) AS paid_cents,
                COALESCE((SELECT COUNT(*) FROM invoices i
                           WHERE i.tenant_id = c.tenant_id AND i.client_id = c.id
                             AND i.status = 'paid'), 0) AS paid_invoice_count,
                (SELECT MAX(i.paid_at) FROM invoices i
                  WHERE i.tenant_id = c.tenant_id AND i.client_id = c.id
                    AND i.status = 'paid') AS latest_paid_at,
                COALESCE((SELECT COUNT(*) FROM projects p
                           JOIN galleries g ON g.project_id = p.id
                            AND g.tenant_id = p.tenant_id
                          WHERE p.tenant_id = c.tenant_id AND p.client_id = c.id
                            AND g.status = 'published'), 0) AS gallery_count,
                (SELECT MAX(g.published_at) FROM projects p
                  JOIN galleries g ON g.project_id = p.id AND g.tenant_id = p.tenant_id
                 WHERE p.tenant_id = c.tenant_id AND p.client_id = c.id
                   AND g.status = 'published') AS latest_gallery_at
              FROM clients c
             WHERE c.tenant_id = ?
          )
         WHERE paid_invoice_count > 0 OR gallery_count > 0
         ORDER BY COALESCE(latest_paid_at, latest_gallery_at, created_at) DESC, id DESC
        """,
        (tenant_id,),
    ).fetchall()
    out: list[dict] = []
    for raw in rows:
        row = dict(raw)
        latest_review = latest_testimonial_for_client(conn, tenant_id, row["id"])
        recent = growth_ask_recent(conn, tenant_id, row["id"])
        status, label, can_send = _opportunity_status(
            {**row, "review_state": _review_state(latest_review)},
            recent=recent,
        )
        row["paid_display"] = money(int(row.get("paid_cents") or 0))
        row["review_state"] = _review_state(latest_review)
        row["last_ask_at"] = last_growth_ask_at(conn, tenant_id, row["id"])
        row["status"] = status
        row["status_label"] = label
        row["can_send"] = can_send
        signals = []
        if int(row.get("paid_invoice_count") or 0):
            signals.append(f"{row['paid_invoice_count']} paid invoice"
                           f"{'' if row['paid_invoice_count'] == 1 else 's'}")
        if int(row.get("gallery_count") or 0):
            signals.append(f"{row['gallery_count']} published galler"
                           f"{'y' if row['gallery_count'] == 1 else 'ies'}")
        if row["review_state"] in ("submitted", "featured"):
            signals.append("review collected")
        elif row["review_state"] == "requested":
            signals.append("review pending")
        row["signal_line"] = " · ".join(signals) or "client activity"
        out.append(row)
    return out[: max(0, int(limit))]


def send_growth_ask(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant_id: str,
    client_id: int,
    actor: str = "owner",
    cooldown_days: int = ASK_COOLDOWN_DAYS,
) -> dict:
    """Send one combined review/referral ask, using audit_log as the cooldown guard."""
    client = get_client(conn, tenant_id, client_id)
    if not client:
        return {"sent": False, "reason": "missing_client"}
    to = (client.get("email") or "").strip()
    if not to:
        return {"sent": False, "reason": "missing_email", "client": client}
    if growth_ask_recent(conn, tenant_id, client_id, cooldown_days=cooldown_days):
        return {"sent": False, "reason": "cooldown", "client": client}

    tenant = get_tenant(conn, tenant_id)
    if not tenant:
        return {"sent": False, "reason": "missing_tenant", "client": client}

    code = referral_code_for(conn, tenant_id, client_id)
    referral_url = referral_link(settings, tenant["slug"], code) if code else ""

    pending = pending_testimonial(conn, tenant_id, client_id)
    latest = latest_testimonial_for_client(conn, tenant_id, client_id)
    review_url = ""
    if pending:
        review_url = testimonial_public_url(settings, pending["token"])
    elif not latest or latest["status"] in ("hidden", "requested"):
        pending = request_testimonial(
            conn,
            tenant_id=tenant_id,
            client_id=client_id,
            author_name=client["name"],
        )
        review_url = testimonial_public_url(settings, pending["token"])

    if review_url:
        review_line = (
            "If you have a minute, a few words about your experience would help future "
            f"clients feel confident booking us:\n{review_url}"
        )
    else:
        review_line = (
            "Your review already helps future clients feel confident booking us. Thank you."
        )

    studio = tenant.get("name") or "your photographer"
    msg = messaging.render(
        conn,
        tenant_id,
        "growth_ask",
        {
            "client": client["name"],
            "studio": studio,
            "review_line": review_line,
            "referral_url": referral_url,
        },
    )
    notify(conn, settings, to=to, tenant_id=tenant_id, subject=msg["subject"], body=msg["body"])
    audit(conn, actor=actor, action="growth.ask_sent", tenant_id=tenant_id,
          detail=_audit_detail(client_id))
    return {
        "sent": True,
        "reason": "sent",
        "client": client,
        "review_url": review_url,
        "referral_url": referral_url,
    }

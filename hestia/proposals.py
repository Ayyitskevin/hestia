"""Client proposals — package + contract + invoice in one shareable booking link.

A proposal is workflow glue, not a second source of truth. The package owns the
service definition, the contract owns terms/signature state, and the invoice owns
money/payment state. The proposal ties those pieces together so a studio can send
one polished link and move a lead toward booking.
"""

from __future__ import annotations

import sqlite3

from . import messaging
from .automations import emit_event
from .config import Settings
from .contracts import create_contract, send_contract, void_contract
from .crypto import new_session_token
from .db import audit
from .email import notify
from .invoices import add_invoice_items, create_invoice, money, send_invoice, void_invoice
from .ownership import mask_invalid_project_id, normalize_client_project_ids
from .packages import get_package

PROPOSAL_STATUSES = ("draft", "sent", "accepted", "declined", "void")


def create_proposal(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant_id: str,
    package_id: int,
    title: str = "",
    summary: str = "",
    terms: str = "",
    client_id: int | None = None,
    project_id: int | None = None,
    signer_name: str = "",
    signer_email: str = "",
) -> dict | None:
    """Create a package-backed proposal plus its draft contract and invoice.

    Returns ``None`` when the package is missing, archived, or the title would be
    blank. Parent ids are normalized to this tenant before any linked records are
    created.
    """
    package = get_package(conn, tenant_id, package_id)
    if not package or not int(package.get("active") or 0):
        return None
    client_id, project_id = normalize_client_project_ids(conn, tenant_id, client_id, project_id)
    label = (title or package["name"] or "").strip()
    if not label:
        return None

    summary_text = (summary or package.get("description") or "").strip()[:2000]
    terms_text = (terms or _default_terms(package, settings.currency)).strip()
    contract = create_contract(
        conn,
        tenant_id=tenant_id,
        title=f"{label} agreement",
        body=terms_text,
        client_id=client_id,
        project_id=project_id,
        signer_name=signer_name,
        signer_email=signer_email,
    )

    amount_cents = int(package.get("deposit_cents") or 0) or int(package.get("price_cents") or 0)
    invoice_title = f"{label} deposit" if int(package.get("deposit_cents") or 0) else label
    invoice = create_invoice(
        conn,
        settings,
        tenant_id=tenant_id,
        title=invoice_title,
        amount_cents=amount_cents,
        client_id=client_id,
        project_id=project_id,
        note=summary_text,
    )
    add_invoice_items(conn, tenant_id, invoice["id"], [(_invoice_item_label(package), amount_cents)])

    token = new_session_token()[:28]
    cur = conn.execute(
        """
        INSERT INTO proposals
            (tenant_id, client_id, project_id, package_id, contract_id, invoice_id,
             title, summary, terms, token)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant_id,
            client_id,
            project_id,
            package["id"],
            contract["id"],
            invoice["id"],
            label[:200],
            summary_text,
            terms_text,
            token,
        ),
    )
    proposal = get_proposal(conn, tenant_id, cur.lastrowid)
    audit(conn, actor="owner", action="proposal.created", tenant_id=tenant_id, detail=label[:200])
    return proposal


def get_proposal(conn: sqlite3.Connection, tenant_id: str, proposal_id: int) -> dict | None:
    row = conn.execute(_proposal_select() + " WHERE pr.id = ? AND pr.tenant_id = ?",
                       (proposal_id, tenant_id)).fetchone()
    return _hydrate(dict(row)) if row else None


def get_proposal_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(_proposal_select() + " WHERE pr.token = ?", (token,)).fetchone()
    return _hydrate(dict(row)) if row else None


def list_proposals(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    status: str | None = None,
) -> list[dict]:
    sql = _proposal_select() + " WHERE pr.tenant_id = ?"
    params: list = [tenant_id]
    if status in PROPOSAL_STATUSES:
        sql += " AND pr.status = ?"
        params.append(status)
    sql += " ORDER BY pr.created_at DESC, pr.id DESC"
    return [_hydrate(dict(r)) for r in conn.execute(sql, params).fetchall()]


def proposal_followups(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    limit: int = 8,
) -> dict:
    """Proposal conversion bottlenecks for the dashboard/list pages.

    ``awaiting_acceptance`` catches sent proposals that have not been accepted.
    ``finish_booking`` catches accepted proposals whose linked contract or invoice
    is still incomplete.
    """
    rows = [
        _hydrate(dict(r)) for r in conn.execute(
            _proposal_select()
            + " WHERE pr.tenant_id = ? AND pr.status IN ('sent', 'accepted') "
            "   AND (pr.status = 'sent' OR ct.status != 'signed' OR i.status != 'paid') "
            " ORDER BY CASE WHEN pr.status = 'accepted' THEN 0 ELSE 1 END, pr.created_at ASC "
            " LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
    ]
    awaiting = [r for r in rows if r["status"] == "sent"]
    finish = [r for r in rows if r["status"] == "accepted"]
    open_cents = sum(int(r.get("invoice_amount_cents") or 0)
                     for r in rows if r.get("invoice_status") != "paid")
    currency = next((r.get("invoice_currency") for r in rows if r.get("invoice_currency")), "usd")
    return {
        "awaiting_acceptance": awaiting,
        "finish_booking": finish,
        "total": len(rows),
        "open_value_cents": open_cents,
        "open_value": money(open_cents, currency),
    }


def proposal_metrics(conn: sqlite3.Connection, tenant_id: str, *, days: int = 30) -> dict:
    """Proposal conversion analytics for the owner dashboard.

    Tracks the recent sent-proposal cohort: sent -> accepted -> paid, average
    time to payment, and money still stuck in open proposal booking invoices.
    """
    window_days = max(1, int(days))
    since = f"-{window_days} days"
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS sent_count,
            COALESCE(SUM(CASE WHEN pr.status = 'accepted' THEN 1 ELSE 0 END), 0)
                AS accepted_count,
            COALESCE(SUM(CASE WHEN pr.status = 'accepted' AND i.status = 'paid' THEN 1 ELSE 0 END), 0)
                AS booked_count,
            COALESCE(SUM(CASE
                WHEN (pr.status = 'sent'
                      OR COALESCE(ct.status, '') != 'signed'
                      OR COALESCE(i.status, '') != 'paid')
                     THEN 1 ELSE 0 END), 0) AS stuck_count,
            COALESCE(SUM(CASE
                WHEN (pr.status = 'sent'
                      OR COALESCE(ct.status, '') != 'signed'
                      OR COALESCE(i.status, '') != 'paid')
                     AND COALESCE(i.status, '') != 'paid'
                     THEN COALESCE(i.amount_cents, 0) ELSE 0 END), 0) AS stuck_cents,
            AVG(CASE
                WHEN pr.status = 'accepted' AND i.status = 'paid' AND i.paid_at IS NOT NULL
                     THEN julianday(i.paid_at) - julianday(COALESCE(pr.sent_at, pr.created_at))
                END) AS avg_time_to_book_days,
            COALESCE(MAX(i.currency), 'usd') AS currency
          FROM proposals pr
          LEFT JOIN contracts ct ON ct.id = pr.contract_id AND ct.tenant_id = pr.tenant_id
          LEFT JOIN invoices i ON i.id = pr.invoice_id AND i.tenant_id = pr.tenant_id
         WHERE pr.tenant_id = ?
           AND pr.status IN ('sent', 'accepted')
           AND COALESCE(pr.sent_at, pr.created_at) >= datetime('now', ?)
        """,
        (tenant_id, since),
    ).fetchone()
    sent = int(row["sent_count"] or 0)
    accepted = int(row["accepted_count"] or 0)
    booked = int(row["booked_count"] or 0)
    stuck = int(row["stuck_count"] or 0)
    stuck_cents = int(row["stuck_cents"] or 0)
    avg_days = row["avg_time_to_book_days"]
    return {
        "window_days": window_days,
        "window_label": f"last {window_days} days",
        "sent_count": sent,
        "accepted_count": accepted,
        "booked_count": booked,
        "stuck_count": stuck,
        "stuck_value_cents": stuck_cents,
        "stuck_value": money(stuck_cents, row["currency"] or "usd"),
        "sent_to_accepted_pct": _pct(accepted, sent),
        "sent_to_accepted": f"{_pct(accepted, sent)}%",
        "accepted_to_paid_pct": _pct(booked, accepted),
        "accepted_to_paid": f"{_pct(booked, accepted)}%",
        "avg_time_to_book_days": float(avg_days) if avg_days is not None else None,
        "avg_time_to_book": _days_display(avg_days),
        "show": bool(sent or stuck),
    }


def send_proposal(conn: sqlite3.Connection, tenant_id: str, proposal_id: int) -> dict | None:
    """Publish a proposal link and make its linked sign/pay links live.

    Idempotent while draft/sent; accepted/void proposals are left untouched.
    """
    proposal = get_proposal(conn, tenant_id, proposal_id)
    if not proposal or proposal["status"] not in ("draft", "sent"):
        return proposal
    send_contract(conn, tenant_id, proposal["contract_id"])
    send_invoice(conn, tenant_id, proposal["invoice_id"])
    conn.execute(
        "UPDATE proposals SET status = 'sent', sent_at = COALESCE(sent_at, datetime('now')), "
        "updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status IN ('draft', 'sent')",
        (proposal_id, tenant_id),
    )
    audit(conn, actor="owner", action="proposal.sent", tenant_id=tenant_id,
          detail=proposal["title"])
    return get_proposal(conn, tenant_id, proposal_id)


def record_proposal_view(conn: sqlite3.Connection, token: str) -> bool:
    """Count a client-facing proposal page view. Draft/void proposals are not counted."""
    cur = conn.execute(
        "UPDATE proposals SET view_count = view_count + 1, last_viewed_at = datetime('now') "
        "WHERE token = ? AND status IN ('sent', 'accepted')",
        (token,),
    )
    return cur.rowcount == 1


def send_proposal_reminder(
    conn: sqlite3.Connection,
    settings: Settings,
    proposal: dict,
) -> str | None:
    """Email the client the single proposal link. Returns send status or None."""
    to = (proposal.get("accepted_email") or proposal.get("client_email") or "").strip()
    if not to:
        return None
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (proposal["tenant_id"],)).fetchone()
    ctx = {
        "client": proposal.get("accepted_name") or proposal.get("client_name") or "there",
        "studio": trow["name"] if trow else "your photographer",
        "title": proposal["title"],
        "proposal_url": proposal_public_url(settings, proposal["token"]),
    }
    msg = messaging.render(conn, proposal["tenant_id"], "proposal_reminder", ctx)
    return notify(conn, settings, to=to, subject=msg["subject"], body=msg["body"],
                  tenant_id=proposal["tenant_id"])


def record_proposal_reminder(conn: sqlite3.Connection, tenant_id: str, proposal_id: int) -> bool:
    """Atomically stamp an actionable proposal reminder.

    A proposal is actionable while it is sent-but-unaccepted, or accepted but the
    linked contract is not signed or linked invoice is not paid. The recipient
    guard prevents counting a reminder that cannot be sent.
    """
    cur = conn.execute(
        """
        UPDATE proposals
           SET last_reminder_at = datetime('now'), reminder_count = reminder_count + 1
         WHERE id = ? AND tenant_id = ?
           AND (
                status = 'sent'
                OR (
                    status = 'accepted'
                    AND (
                        EXISTS (
                            SELECT 1 FROM contracts ct
                             WHERE ct.id = proposals.contract_id
                               AND ct.tenant_id = proposals.tenant_id
                               AND ct.status != 'signed'
                        )
                        OR EXISTS (
                            SELECT 1 FROM invoices i
                             WHERE i.id = proposals.invoice_id
                               AND i.tenant_id = proposals.tenant_id
                               AND i.status != 'paid'
                        )
                    )
                )
           )
           AND (
                TRIM(COALESCE(accepted_email, '')) <> ''
                OR EXISTS (
                    SELECT 1 FROM clients c
                     WHERE c.id = proposals.client_id
                       AND c.tenant_id = proposals.tenant_id
                       AND TRIM(COALESCE(c.email, '')) <> ''
                )
           )
        """,
        (proposal_id, tenant_id),
    )
    return cur.rowcount == 1


def accept_proposal(
    conn: sqlite3.Connection,
    *,
    token: str,
    accepted_name: str,
    accepted_email: str = "",
) -> bool:
    """Idempotently accept a proposal. Returns True only for ``sent → accepted``."""
    name = (accepted_name or "").strip()
    if not name:
        return False
    cur = conn.execute(
        "UPDATE proposals SET status = 'accepted', accepted_name = ?, accepted_email = ?, "
        "accepted_at = datetime('now'), updated_at = datetime('now') "
        "WHERE token = ? AND status = 'sent'",
        (name[:200], (accepted_email or "").strip()[:200], token),
    )
    if cur.rowcount == 0:
        return False
    row = conn.execute(
        "SELECT tenant_id, title, client_id, project_id FROM proposals WHERE token = ?",
        (token,),
    ).fetchone()
    audit(conn, actor="client", action="proposal.accepted", tenant_id=row["tenant_id"],
          detail=f"{row['title']} · accepted by {name[:200]}")
    emit_event(conn, tenant_id=row["tenant_id"], event="proposal.accepted",
               context={"client_id": row["client_id"], "project_id": row["project_id"],
                        "title": row["title"]})
    return True


def void_proposal(conn: sqlite3.Connection, tenant_id: str, proposal_id: int) -> None:
    """Void an unaccepted proposal and try to void its draft/sent linked records."""
    proposal = get_proposal(conn, tenant_id, proposal_id)
    if not proposal or proposal["status"] == "accepted":
        return
    void_contract(conn, tenant_id, proposal["contract_id"])
    void_invoice(conn, tenant_id, proposal["invoice_id"])
    cur = conn.execute(
        "UPDATE proposals SET status = 'void', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status != 'accepted'",
        (proposal_id, tenant_id),
    )
    if cur.rowcount:
        audit(conn, actor="owner", action="proposal.void", tenant_id=tenant_id,
              detail=proposal["title"])


def proposal_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/proposal/{token}"


def _proposal_select() -> str:
    return (
        "SELECT pr.*, c.name AS client_name, c.email AS client_email, "
        "       p.id AS valid_project_id, p.name AS project_name, "
        "       sp.name AS package_name, sp.description AS package_description, "
        "       sp.price_cents AS package_price_cents, sp.deposit_cents AS package_deposit_cents, "
        "       ct.status AS contract_status, ct.token AS contract_token, "
        "       i.status AS invoice_status, i.token AS invoice_token, "
        "       i.amount_cents AS invoice_amount_cents, i.currency AS invoice_currency "
        "  FROM proposals pr "
        "  LEFT JOIN clients c ON c.id = pr.client_id AND c.tenant_id = pr.tenant_id "
        "  LEFT JOIN projects p ON p.id = pr.project_id AND p.tenant_id = pr.tenant_id "
        "   AND (pr.client_id IS NULL OR p.client_id = pr.client_id) "
        "  LEFT JOIN service_packages sp ON sp.id = pr.package_id AND sp.tenant_id = pr.tenant_id "
        "  LEFT JOIN contracts ct ON ct.id = pr.contract_id AND ct.tenant_id = pr.tenant_id "
        "  LEFT JOIN invoices i ON i.id = pr.invoice_id AND i.tenant_id = pr.tenant_id "
    )


def _hydrate(row: dict) -> dict:
    row = mask_invalid_project_id(row)
    currency = row.get("invoice_currency") or "usd"
    price = int(row.get("package_price_cents") or 0)
    deposit = int(row.get("package_deposit_cents") or 0)
    due = int(row.get("invoice_amount_cents") or 0)
    row["package_price_display"] = money(price, currency)
    row["package_deposit_display"] = money(deposit, currency) if deposit else ""
    row["invoice_amount_display"] = money(due, currency)
    row["needs_signature"] = row.get("status") == "accepted" and row.get("contract_status") != "signed"
    row["needs_payment"] = row.get("status") == "accepted" and row.get("invoice_status") != "paid"
    row["needs_acceptance"] = row.get("status") == "sent"
    row["needs_followup"] = bool(row["needs_acceptance"] or row["needs_signature"] or row["needs_payment"])
    missing = []
    if row["needs_acceptance"]:
        missing.append("acceptance")
    if row["needs_signature"]:
        missing.append("signature")
    if row["needs_payment"]:
        missing.append("payment")
    row["followup_label"] = "Needs " + " + ".join(missing) if missing else "Complete"
    row["reminder_email"] = (row.get("accepted_email") or row.get("client_email") or "").strip()
    row.update(_next_action(row))
    return row


def _next_action(row: dict) -> dict:
    status = row.get("status")
    views = int(row.get("view_count") or 0)
    reminders = int(row.get("reminder_count") or 0)
    if status == "draft":
        return {"next_action": "Publish proposal",
                "next_action_detail": "Make the proposal link live for the client."}
    if status == "sent":
        if views == 0:
            detail = "Not viewed yet"
            if reminders:
                detail += f" after {reminders} reminder{'s' if reminders != 1 else ''}"
            return {"next_action": "Confirm delivery",
                    "next_action_detail": f"{detail}. Resend the proposal or check the client's email."}
        return {"next_action": "Nudge acceptance",
                "next_action_detail": "Viewed but not accepted. A short personal follow-up is the next move."}
    if status == "accepted":
        if row.get("needs_signature") and row.get("needs_payment"):
            return {"next_action": "Finish booking",
                    "next_action_detail": "Accepted, but signature and booking invoice are still open."}
        if row.get("needs_signature"):
            return {"next_action": "Collect signature",
                    "next_action_detail": "Payment is handled; chase the agreement signature."}
        if row.get("needs_payment"):
            return {"next_action": "Collect payment",
                    "next_action_detail": "Agreement is signed; chase the booking invoice."}
        return {"next_action": "Booked",
                "next_action_detail": "Accepted, signed, and paid."}
    if status == "void":
        return {"next_action": "Voided", "next_action_detail": "No client action needed."}
    return {"next_action": status or "Unknown", "next_action_detail": ""}


def _pct(part: int, whole: int) -> int:
    return round(100 * int(part) / int(whole)) if whole else 0


def _days_display(value) -> str:
    if value is None:
        return "-"
    days = max(0.0, float(value))
    if days < 1:
        return "<1 day"
    label = "day" if round(days, 1) == 1 else "days"
    return f"{days:.1f} {label}"


def _invoice_item_label(package: dict) -> str:
    return f"{package['name']} booking deposit" if int(package.get("deposit_cents") or 0) else package["name"]


def _default_terms(package: dict, currency: str) -> str:
    price = money(int(package.get("price_cents") or 0), currency)
    deposit_cents = int(package.get("deposit_cents") or 0)
    retainer = money(deposit_cents, currency) if deposit_cents else price
    included = (package.get("description") or "Photography services described in this proposal.").strip()
    return (
        f"{package['name']}\n\n"
        f"Included:\n{included}\n\n"
        f"Package price: {price}\n"
        f"Due to reserve: {retainer}\n\n"
        "Booking is reserved when this proposal is accepted, the agreement is signed, "
        "and the initial invoice is paid. Remaining balances follow the studio's invoice schedule."
    )

"""Client proposals — package + contract + invoice in one shareable booking link.

A proposal is workflow glue, not a second source of truth. The package owns the
service definition, the contract owns terms/signature state, and the invoice owns
money/payment state. The proposal ties those pieces together so a studio can send
one polished link and move a lead toward booking.
"""

from __future__ import annotations

import sqlite3

from .automations import emit_event
from .config import Settings
from .contracts import create_contract, send_contract, void_contract
from .crypto import new_session_token
from .db import audit
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
        "UPDATE proposals SET status = 'sent', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND status IN ('draft', 'sent')",
        (proposal_id, tenant_id),
    )
    audit(conn, actor="owner", action="proposal.sent", tenant_id=tenant_id,
          detail=proposal["title"])
    return get_proposal(conn, tenant_id, proposal_id)


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
    return row


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

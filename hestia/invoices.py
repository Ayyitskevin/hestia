"""Invoice data access — bill a client/project and collect via the payments seam.

Statuses: ``draft → sent → paid`` (or ``void``). Each invoice carries a public
``token`` for a shareable pay link. Marking paid is idempotent — a double
callback never double-settles. Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .crypto import new_session_token
from .db import audit

INVOICE_STATUSES = ("draft", "sent", "paid", "void")


def money(cents: int, currency: str = "usd") -> str:
    sym = {"usd": "$", "eur": "€", "gbp": "£"}.get(currency, "")
    return f"{sym}{cents / 100:,.2f}"


def create_invoice(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant_id: str,
    title: str,
    amount_cents: int,
    client_id: int | None = None,
    project_id: int | None = None,
) -> dict:
    token = new_session_token()[:28]
    cur = conn.execute(
        """
        INSERT INTO invoices (tenant_id, client_id, project_id, title, amount_cents, currency, token)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, project_id, title.strip(), max(0, int(amount_cents)),
         settings.currency, token),
    )
    return get_invoice(conn, tenant_id, cur.lastrowid)


def get_invoice(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT i.*, c.name AS client_name, c.email AS client_email, p.name AS project_name
          FROM invoices i
          LEFT JOIN clients c ON c.id = i.client_id
          LEFT JOIN projects p ON p.id = i.project_id
         WHERE i.id = ? AND i.tenant_id = ?
        """,
        (invoice_id, tenant_id),
    ).fetchone()
    return _hydrate(dict(row)) if row else None


def get_invoice_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM invoices WHERE token = ?", (token,)).fetchone()
    return _hydrate(dict(row)) if row else None


def list_invoices(
    conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None
) -> list[dict]:
    sql = (
        "SELECT i.*, c.name AS client_name, p.name AS project_name "
        "  FROM invoices i LEFT JOIN clients c ON c.id = i.client_id "
        "  LEFT JOIN projects p ON p.id = i.project_id WHERE i.tenant_id = ?"
    )
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND i.project_id = ?"
        params.append(project_id)
    sql += " ORDER BY i.created_at DESC"
    return [_hydrate(dict(r)) for r in conn.execute(sql, params).fetchall()]


def send_invoice(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> None:
    conn.execute(
        "UPDATE invoices SET status = 'sent' WHERE id = ? AND tenant_id = ? AND status = 'draft'",
        (invoice_id, tenant_id),
    )


def void_invoice(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> None:
    conn.execute(
        "UPDATE invoices SET status = 'void' WHERE id = ? AND tenant_id = ? AND status != 'paid'",
        (invoice_id, tenant_id),
    )


def mark_paid(conn: sqlite3.Connection, *, token: str, provider: str, ref: str) -> bool:
    """Idempotently settle an invoice. Returns True only on the transition to paid."""
    row = conn.execute(
        "SELECT tenant_id, title, amount_cents, currency, status FROM invoices WHERE token = ?",
        (token,),
    ).fetchone()
    if not row or row["status"] == "paid":
        return False
    conn.execute(
        "UPDATE invoices SET status = 'paid', provider = ?, provider_ref = ?, "
        "paid_at = datetime('now') WHERE token = ?",
        (provider, ref, token),
    )
    audit(conn, actor=f"payment:{provider}", action="invoice.paid", tenant_id=row["tenant_id"],
          detail=f"{row['title']} · {money(row['amount_cents'], row['currency'])}")
    return True


def _hydrate(row: dict) -> dict:
    row["amount_display"] = money(row["amount_cents"], row.get("currency", "usd"))
    return row


def invoice_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/pay/{token}"

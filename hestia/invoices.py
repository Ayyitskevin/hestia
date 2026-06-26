"""Invoice data access — bill a client/project and collect via the payments seam.

Statuses: ``draft → sent → paid`` (or ``void``). Each invoice carries a public
``token`` for a shareable pay link. Marking paid is idempotent — a double
callback never double-settles. Tenant-scoped throughout.
"""

from __future__ import annotations

import sqlite3

from .automations import emit_event
from .config import Settings
from .crypto import new_session_token
from .db import audit
from .email import notify

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
    plan_id: int | None = None,
    due_date: str = "",
    sequence: int = 0,
) -> dict:
    token = new_session_token()[:28]
    cur = conn.execute(
        """
        INSERT INTO invoices
            (tenant_id, client_id, project_id, title, amount_cents, currency, token,
             plan_id, due_date, sequence)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, project_id, title.strip(), max(0, int(amount_cents)),
         settings.currency, token, plan_id, due_date.strip(), int(sequence)),
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
    conn: sqlite3.Connection, tenant_id: str, *,
    project_id: int | None = None, client_id: int | None = None, standalone_only: bool = False
) -> list[dict]:
    sql = (
        "SELECT i.*, c.name AS client_name, p.name AS project_name, "
        # an invoice still 'sent' past its due_date is overdue; date() yields NULL
        # for the free-text due dates owners may type, so those are never "overdue"
        "  CASE WHEN i.status = 'sent' AND date(i.due_date) IS NOT NULL "
        "       AND date(i.due_date) < date('now') THEN 1 ELSE 0 END AS is_overdue, "
        "  CASE WHEN i.status = 'sent' AND date(i.due_date) IS NOT NULL "
        "       AND date(i.due_date) < date('now') "
        "       THEN CAST(julianday('now') - julianday(date(i.due_date)) AS INTEGER) END AS days_overdue "
        "  FROM invoices i LEFT JOIN clients c ON c.id = i.client_id "
        "  LEFT JOIN projects p ON p.id = i.project_id WHERE i.tenant_id = ?"
    )
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND i.project_id = ?"
        params.append(project_id)
    if client_id is not None:
        sql += " AND i.client_id = ?"
        params.append(client_id)
    if standalone_only:
        # Plan installments surface under their payment plan, not the flat list.
        sql += " AND i.plan_id IS NULL"
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
        "SELECT tenant_id, title, amount_cents, currency, status, client_id, project_id "
        "FROM invoices WHERE token = ?",
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
    emit_event(conn, tenant_id=row["tenant_id"], event="invoice.paid",
               context={"client_id": row["client_id"], "project_id": row["project_id"],
                        "title": row["title"]})
    return True


def _hydrate(row: dict) -> dict:
    row["amount_display"] = money(row["amount_cents"], row.get("currency", "usd"))
    return row


def invoice_public_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/pay/{token}"


# --- accounts receivable: see what's owed, chase what's late ------------------

# 'sent' (not draft/paid/void) and past a parseable due_date → overdue.
_OVERDUE_SQL = "date(due_date) IS NOT NULL AND date(due_date) < date('now')"


def accounts_receivable(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Outstanding (sent, unpaid) money and the overdue subset, cents + display."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount_cents), 0) AS outstanding, COUNT(*) AS outstanding_count, "
        f"  COALESCE(SUM(CASE WHEN {_OVERDUE_SQL} THEN amount_cents ELSE 0 END), 0) AS overdue, "
        f"  COALESCE(SUM(CASE WHEN {_OVERDUE_SQL} THEN 1 ELSE 0 END), 0) AS overdue_count "
        "FROM invoices WHERE tenant_id = ? AND status = 'sent'",
        (tenant_id,),
    ).fetchone()
    return {
        "outstanding_cents": int(row["outstanding"]), "outstanding_count": int(row["outstanding_count"]),
        "overdue_cents": int(row["overdue"]), "overdue_count": int(row["overdue_count"]),
        "outstanding": money(int(row["outstanding"])), "overdue": money(int(row["overdue"])),
    }


def send_invoice_reminder(conn: sqlite3.Connection, settings: Settings, invoice: dict) -> str | None:
    """Email the client a friendly past-due nudge with the pay link. Returns the
    send status, or None when there's no client email to send to."""
    to = (invoice.get("client_email") or "").strip()
    if not to:
        return None
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (invoice["tenant_id"],)).fetchone()
    studio = trow["name"] if trow else "your photographer"
    amount = invoice.get("amount_display") or money(invoice["amount_cents"], invoice.get("currency", "usd"))
    pay_url = invoice_public_url(settings, invoice["token"])
    subject = f'Reminder: invoice "{invoice["title"]}" is past due'
    body = (
        f"Hi {invoice.get('client_name') or 'there'},\n\n"
        f'A friendly reminder that your invoice from {studio} — "{invoice["title"]}" '
        f"for {amount} — is now past due.\n\n"
        f"You can pay securely here:\n{pay_url}\n\n"
        f"Thank you!\n{studio}"
    )
    return notify(conn, settings, to=to, subject=subject, body=body, tenant_id=invoice["tenant_id"])


def record_invoice_reminder(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> bool:
    """Stamp a reminder as sent — idempotent bookkeeping that gates the next auto
    nudge. Only stamps a still-'sent' invoice; True if a row was updated."""
    cur = conn.execute(
        "UPDATE invoices SET last_reminder_at = datetime('now'), reminder_count = reminder_count + 1 "
        "WHERE id = ? AND tenant_id = ? AND status = 'sent'",
        (invoice_id, tenant_id),
    )
    return cur.rowcount > 0


def send_overdue_reminders(conn: sqlite3.Connection, settings: Settings, *,
                           cooldown_days: int = 7, limit: int = 500) -> int:
    """Across all tenants, nudge each overdue invoice not reminded within the
    cooldown window, then stamp it so the next sweep leaves it alone until the
    window passes again. The cooldown is what keeps a daily sweep from spamming.
    Returns the number of reminders actually sent."""
    rows = conn.execute(
        "SELECT i.id, i.tenant_id, i.title, i.amount_cents, i.currency, i.token, "
        "       c.name AS client_name, c.email AS client_email "
        "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        f"WHERE i.status = 'sent' AND {_OVERDUE_SQL.replace('due_date', 'i.due_date')} "
        "  AND (i.last_reminder_at IS NULL OR i.last_reminder_at < datetime('now', ?)) "
        "ORDER BY i.id LIMIT ?",
        (f"-{int(cooldown_days)} days", limit),
    ).fetchall()
    sent = 0
    for r in rows:
        inv = _hydrate(dict(r))
        if send_invoice_reminder(conn, settings, inv):
            record_invoice_reminder(conn, inv["tenant_id"], inv["id"])
            sent += 1
    return sent

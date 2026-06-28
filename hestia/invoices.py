"""Invoice data access — bill a client/project and collect via the payments seam.

Statuses: ``draft → sent → paid`` (or ``void``). Each invoice carries a public
``token`` for a shareable pay link. Marking paid is idempotent — a double
callback never double-settles. Tenant-scoped throughout.
"""

from __future__ import annotations

import datetime
import sqlite3

from . import messaging
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
    tax_cents: int = 0,
    note: str = "",
) -> dict:
    token = new_session_token()[:28]
    cur = conn.execute(
        """
        INSERT INTO invoices
            (tenant_id, client_id, project_id, title, amount_cents, currency, token,
             plan_id, due_date, sequence, tax_cents, note)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, project_id, title.strip(), max(0, int(amount_cents)),
         settings.currency, token, plan_id, due_date.strip(), int(sequence), max(0, int(tax_cents)),
         note.strip()[:1000]),
    )
    return get_invoice(conn, tenant_id, cur.lastrowid)


def set_invoice_note(conn: sqlite3.Connection, tenant_id: str, invoice_id: int, note: str) -> None:
    """Set an invoice's personal note (shown on the pay page, sent in the email).
    Display only — never touches the amount, tax, or status."""
    conn.execute(
        "UPDATE invoices SET note = ? WHERE id = ? AND tenant_id = ?",
        (note.strip()[:1000], invoice_id, tenant_id),
    )


def add_invoice_items(conn: sqlite3.Connection, tenant_id: str, invoice_id: int,
                      items: list[tuple[str, int]]) -> None:
    """Attach line items (description, amount_cents) to an invoice, in order. The caller
    sets the invoice's amount_cents to their sum — these rows are the display breakdown.
    Amounts may be negative (a discount line); the caller floors the subtotal at zero."""
    for pos, (desc, cents) in enumerate(items, start=1):
        conn.execute(
            "INSERT INTO invoice_items (invoice_id, tenant_id, description, amount_cents, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (invoice_id, tenant_id, (desc or "").strip()[:300], int(cents), pos),
        )


def invoice_items(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> list[dict]:
    """An invoice's line items (empty for a flat single-amount invoice), with displays.
    The currency comes from the parent invoice via a tenant-matched join."""
    rows = conn.execute(
        "SELECT it.*, i.currency FROM invoice_items it "
        "JOIN invoices i ON i.id = it.invoice_id AND i.tenant_id = it.tenant_id "
        "WHERE it.invoice_id = ? AND it.tenant_id = ? ORDER BY it.position, it.id",
        (invoice_id, tenant_id),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["amount_display"] = money(d["amount_cents"], d.get("currency") or "usd")
        out.append(d)
    return out


def duplicate_invoice(conn: sqlite3.Connection, settings: Settings, tenant_id: str,
                      invoice_id: int) -> dict | None:
    """Clone an invoice into a fresh draft — same title, amounts, tax, client/project,
    note, and line items — with a new token and no payment state. For repeat/retainer
    billing. Returns the new invoice, or None if the source isn't this tenant's. The
    clone is standalone (never a plan installment)."""
    src = get_invoice(conn, tenant_id, invoice_id)
    if not src:
        return None
    new = create_invoice(
        conn, settings, tenant_id=tenant_id, title=src["title"],
        amount_cents=int(src["amount_cents"]), client_id=src.get("client_id"),
        project_id=src.get("project_id"), tax_cents=int(src.get("tax_cents") or 0),
        note=src.get("note") or "",
    )
    items = invoice_items(conn, tenant_id, invoice_id)
    if items:
        add_invoice_items(conn, tenant_id, new["id"],
                          [(it["description"], it["amount_cents"]) for it in items])
        new = get_invoice(conn, tenant_id, new["id"])
    return new


def tax_for(amount_cents: int, rate_bps: int) -> int:
    """Sales tax in cents for a subtotal at a basis-point rate (850 = 8.50%)."""
    return round(max(0, int(amount_cents)) * max(0, int(rate_bps)) / 10000)


def get_invoice(conn: sqlite3.Connection, tenant_id: str, invoice_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT i.*, c.name AS client_name, c.email AS client_email, p.name AS project_name
          FROM invoices i
          -- tenant-match the joins: an invoice carrying another studio's client_id /
          -- project_id (IDs are global) must not surface that studio's name or email
          LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id
          LEFT JOIN projects p ON p.id = i.project_id AND p.tenant_id = i.tenant_id
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
    project_id: int | None = None, client_id: int | None = None, standalone_only: bool = False,
    status: str | None = None,
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
        # tenant-match the joins so a stray cross-tenant client_id/project_id can't
        # surface another studio's client or project name in this list
        "  FROM invoices i LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        "  LEFT JOIN projects p ON p.id = i.project_id AND p.tenant_id = i.tenant_id "
        "  WHERE i.tenant_id = ?"
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
    if status == "overdue":          # pseudo-status: still 'sent' and past a real due date
        sql += " AND i.status = 'sent' AND date(i.due_date) IS NOT NULL AND date(i.due_date) < date('now')"
    elif status in ("draft", "sent", "paid", "void"):
        sql += " AND i.status = ?"
        params.append(status)
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
    # Atomic claim: the status guard + rowcount is the real idempotency barrier — the
    # pre-read above is only a fast path. Two near-concurrent callbacks (Stripe retries
    # at-least-once) can both pass that read; only the one whose UPDATE actually flips
    # 'paid' settles, so invoice.paid never audits or emits twice for one payment.
    cur = conn.execute(
        "UPDATE invoices SET status = 'paid', provider = ?, provider_ref = ?, "
        "paid_at = datetime('now') WHERE token = ? AND status != 'paid'",
        (provider, ref, token),
    )
    if cur.rowcount == 0:           # lost the race — already settled by another caller
        return False
    audit(conn, actor=f"payment:{provider}", action="invoice.paid", tenant_id=row["tenant_id"],
          detail=f"{row['title']} · {money(row['amount_cents'], row['currency'])}")
    emit_event(conn, tenant_id=row["tenant_id"], event="invoice.paid",
               context={"client_id": row["client_id"], "project_id": row["project_id"],
                        "title": row["title"]})
    return True


OFFLINE_METHODS = ("cash", "check", "bank transfer", "other")


def record_offline_payment(conn: sqlite3.Connection, tenant_id: str, invoice_id: int, *,
                           method: str = "other") -> bool:
    """Owner-side: record a payment taken outside the online pay link — cash, check, a
    bank transfer. Idempotently settles the invoice, returning True only on the single
    transition to paid. Tenant-scoped; only a draft or sent invoice can be recorded paid
    (never a void or already-paid one). Fires invoice.paid the same as an online payment,
    so downstream automations don't care how the money arrived."""
    label = (method or "other").strip().lower()
    if label not in OFFLINE_METHODS:
        label = "other"
    row = conn.execute(
        "SELECT title, amount_cents, currency, status, client_id, project_id "
        "FROM invoices WHERE id = ? AND tenant_id = ?",
        (invoice_id, tenant_id),
    ).fetchone()
    if not row or row["status"] not in ("draft", "sent"):
        return False
    # Same atomic claim as mark_paid: the status guard + rowcount is the idempotency
    # barrier, so a double-click can't settle (or audit/emit) the same invoice twice.
    cur = conn.execute(
        "UPDATE invoices SET status = 'paid', provider = ?, provider_ref = 'offline', "
        "paid_at = datetime('now') WHERE id = ? AND tenant_id = ? AND status IN ('draft', 'sent')",
        (label, invoice_id, tenant_id),
    )
    if cur.rowcount == 0:
        return False
    audit(conn, actor="owner", action="invoice.paid", tenant_id=tenant_id,
          detail=f"{row['title']} · {money(row['amount_cents'], row['currency'])} · {label} (manual)")
    emit_event(conn, tenant_id=tenant_id, event="invoice.paid",
               context={"client_id": row["client_id"], "project_id": row["project_id"],
                        "title": row["title"]})
    return True


def _hydrate(row: dict) -> dict:
    cur = row.get("currency", "usd")
    tax = int(row.get("tax_cents") or 0)            # rows that don't select it → no tax
    row["amount_display"] = money(row["amount_cents"], cur)   # the pre-tax subtotal
    row["tax_cents"] = tax
    row["tax_display"] = money(tax, cur)
    row["total_cents"] = int(row["amount_cents"]) + tax
    row["total_display"] = money(row["total_cents"], cur)     # what the client pays
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
        # plan_id IS NULL: plan installments are tracked under their payment plan, not
        # the flat invoices list — so A/R here matches what that list actually shows
        "FROM invoices WHERE tenant_id = ? AND status = 'sent' AND plan_id IS NULL",
        (tenant_id,),
    ).fetchone()
    return {
        "outstanding_cents": int(row["outstanding"]), "outstanding_count": int(row["outstanding_count"]),
        "overdue_cents": int(row["overdue"]), "overdue_count": int(row["overdue_count"]),
        "outstanding": money(int(row["outstanding"])), "overdue": money(int(row["overdue"])),
    }


def _is_overdue(due_date) -> bool:
    """True if a parseable ISO due_date is before today — same notion of "overdue"
    the SQL date() comparison uses. Empty/free-text dates count as not overdue."""
    if not due_date:
        return False
    try:
        return datetime.date.fromisoformat(str(due_date).strip()) < datetime.date.today()
    except ValueError:
        return False


def send_invoice_reminder(conn: sqlite3.Connection, settings: Settings, invoice: dict) -> str | None:
    """Email the client a payment nudge with the pay link. The wording adapts to
    whether the invoice is actually past due, so a manual nudge on a not-yet-due
    invoice doesn't falsely claim it's overdue. Returns the send status, or None
    when there's no client email to send to."""
    to = (invoice.get("client_email") or "").strip()
    if not to:
        return None
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (invoice["tenant_id"],)).fetchone()
    ctx = {
        "client": invoice.get("client_name") or "there",
        "studio": trow["name"] if trow else "your photographer",
        "title": invoice["title"],
        "amount": invoice.get("amount_display") or money(invoice["amount_cents"],
                                                         invoice.get("currency", "usd")),
        "pay_url": invoice_public_url(settings, invoice["token"]),
    }
    # past-due vs not-yet-due get their own template, so a manual nudge on a current
    # invoice never falsely says "past due".
    kind = "invoice_overdue" if _is_overdue(invoice.get("due_date")) else "invoice_reminder"
    msg = messaging.render(conn, invoice["tenant_id"], kind, ctx)
    return notify(conn, settings, to=to, subject=msg["subject"], body=msg["body"],
                  tenant_id=invoice["tenant_id"])


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
    """Across all tenants, nudge each overdue invoice whose client has an email on
    file and who hasn't been reminded within the cooldown window. Returns reminders
    sent.

    Two correctness guards:
    - The inner JOIN + non-empty email filter means an invoice with no client (or no
      client email) is never selected — so it can't be rescanned every sweep forever.
    - Each invoice is *claimed* first (record_invoice_reminder is an atomic UPDATE
      gated on status='sent'); only a successful claim sends. An invoice paid between
      this SELECT and the send therefore can't receive a duplicate reminder."""
    rows = conn.execute(
        "SELECT i.id, i.tenant_id, i.title, i.amount_cents, i.currency, i.token, i.due_date, "
        "       c.name AS client_name, c.email AS client_email "
        "FROM invoices i JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        f"WHERE i.status = 'sent' AND {_OVERDUE_SQL.replace('due_date', 'i.due_date')} "
        "  AND TRIM(COALESCE(c.email, '')) <> '' "
        "  AND (i.last_reminder_at IS NULL OR i.last_reminder_at < datetime('now', ?)) "
        "ORDER BY i.id LIMIT ?",
        (f"-{int(cooldown_days)} days", limit),
    ).fetchall()
    sent = 0
    for r in rows:
        inv = _hydrate(dict(r))
        if record_invoice_reminder(conn, inv["tenant_id"], inv["id"]):   # claim before send
            send_invoice_reminder(conn, settings, inv)
            sent += 1
    return sent

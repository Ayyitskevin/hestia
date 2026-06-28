"""Recurring invoices — retainer / subscription-style billing on a cadence.

A recurring *profile* is an invoice template (title, amount, client, note) plus a
cadence and a ``next_run_at`` date. A periodic worker sweep (:func:`run_recurring`)
generates the next invoice when a profile comes due and advances ``next_run_at`` by one
period — **atomically**, so two concurrent sweeps can never bill the same period twice.
Each generated invoice is an ordinary invoice (its own pay link, the same idempotent
settle path); the profile just spawns them on schedule.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .db import audit

CADENCES = {"weekly": "+7 days", "monthly": "+1 month", "yearly": "+1 year"}
CADENCE_LABELS = {"weekly": "Weekly", "monthly": "Monthly", "yearly": "Yearly"}

# Advance next_run_at by exactly one period. Monthly clamps to the last day of the target
# month — so an end-of-month date (Jan 31) advances to Feb 28/29 instead of SQLite's raw
# date(x,'+1 month') overflowing to Mar 03 and skipping a month entirely.
_ADVANCE_SQL = (
    "CASE cadence "
    "WHEN 'weekly' THEN date(next_run_at, '+7 days') "
    "WHEN 'yearly' THEN date(next_run_at, '+1 year') "
    "ELSE min(date(next_run_at, '+1 month'), "
    "         date(next_run_at, 'start of month', '+2 months', '-1 day')) END"
)


def create_recurring(
    conn: sqlite3.Connection, *, tenant_id: str, title: str, amount_cents: int,
    cadence: str = "monthly", next_run_at: str = "", client_id: int | None = None,
    project_id: int | None = None, note: str = "",
) -> dict | None:
    """Create a recurring profile. The first invoice generates on ``next_run_at`` (a
    YYYY-MM-DD date; defaults to today, and an unparseable value also falls back to
    today). Returns None for a blank title."""
    title = (title or "").strip()
    if not title:
        return None
    cadence = cadence if cadence in CADENCES else "monthly"
    # Floor the first run at today: a past 'Starting' date must not put the profile in
    # arrears and trigger a catch-up storm of back-dated invoices on the first sweep.
    cur = conn.execute(
        "INSERT INTO recurring_invoices (tenant_id, title, amount_cents, client_id, "
        "project_id, note, cadence, next_run_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, max(COALESCE(date(?), date('now')), date('now')))",
        (tenant_id, title[:200], max(0, int(amount_cents)), client_id, project_id,
         (note or "").strip()[:1000], cadence, (next_run_at or "").strip() or None),
    )
    return get_recurring(conn, tenant_id, cur.lastrowid)


def get_recurring(conn: sqlite3.Connection, tenant_id: str, recurring_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM recurring_invoices WHERE id = ? AND tenant_id = ?",
        (recurring_id, tenant_id),
    ).fetchone()
    return dict(row) if row else None


def list_recurring(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """All of a tenant's recurring profiles (active first), with the client name and a
    cadence label for display."""
    rows = conn.execute(
        "SELECT r.*, c.name AS client_name FROM recurring_invoices r "
        "LEFT JOIN clients c ON c.id = r.client_id AND c.tenant_id = r.tenant_id "
        "WHERE r.tenant_id = ? ORDER BY r.active DESC, r.next_run_at, r.id",
        (tenant_id,),
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["cadence_label"] = CADENCE_LABELS.get(d["cadence"], d["cadence"])
        out.append(d)
    return out


def set_recurring_active(conn: sqlite3.Connection, tenant_id: str, recurring_id: int,
                         active: bool) -> None:
    """Pause (active=False) or resume (active=True) a profile — tenant-scoped."""
    conn.execute(
        "UPDATE recurring_invoices SET active = ? WHERE id = ? AND tenant_id = ?",
        (1 if active else 0, recurring_id, tenant_id),
    )


def delete_recurring(conn: sqlite3.Connection, tenant_id: str, recurring_id: int) -> None:
    conn.execute(
        "DELETE FROM recurring_invoices WHERE id = ? AND tenant_id = ?",
        (recurring_id, tenant_id),
    )


def _claim_due(conn: sqlite3.Connection, recurring_id: int) -> bool:
    """Atomically claim a due profile: advance next_run_at by one period and stamp it.
    The WHERE guard (active AND date(next_run_at) <= today) plus rowcount is the
    idempotency barrier — only the sweep whose UPDATE actually advances the date goes on
    to bill, so a second concurrent sweep claims nothing and never double-bills."""
    cur = conn.execute(
        f"UPDATE recurring_invoices SET next_run_at = {_ADVANCE_SQL}, "
        "last_invoiced_at = datetime('now'), invoice_count = invoice_count + 1 "
        "WHERE id = ? AND active = 1 AND date(next_run_at) <= date('now')",
        (recurring_id,),
    )
    return cur.rowcount == 1


def _bill_profile(conn: sqlite3.Connection, settings: Settings, prof: dict) -> dict | None:
    """Create + mark-sent the invoice for a claimed profile (with tax + audit). Money write
    ONLY — the client email is sent separately, *after* this is committed, so a failed
    send can never roll back the bill and cause a re-bill. Returns the hydrated invoice."""
    from .invoices import create_invoice, get_invoice, send_invoice, tax_for

    tid = prof["tenant_id"]
    trow = conn.execute("SELECT tax_rate_bps FROM tenants WHERE id = ?", (tid,)).fetchone()
    tax = tax_for(prof["amount_cents"], (trow["tax_rate_bps"] if trow else 0) or 0)
    invoice = create_invoice(
        conn, settings, tenant_id=tid, title=prof["title"], amount_cents=prof["amount_cents"],
        client_id=prof["client_id"], project_id=prof["project_id"], tax_cents=tax,
        note=prof.get("note") or "",
    )
    send_invoice(conn, tid, invoice["id"])      # generated invoices go out as 'sent'
    inv = get_invoice(conn, tid, invoice["id"])
    audit(conn, actor="system", action="invoice.recurring", tenant_id=tid,
          detail=f"{prof['title']} · {inv['amount_display'] if inv else ''}")
    return inv


def _email_invoice(conn: sqlite3.Connection, settings: Settings, prof: dict, inv: dict) -> None:
    """Email the client their pay link for a just-generated invoice. Best-effort and run
    AFTER the bill is committed, so an at-least-once send can never double-bill."""
    from . import messaging
    from .email import notify
    from .invoices import invoice_public_url

    if not (inv and inv.get("client_email")):
        return
    tid = prof["tenant_id"]
    trow = conn.execute("SELECT name FROM tenants WHERE id = ?", (tid,)).fetchone()
    note = (prof.get("note") or "").strip()
    ctx = {
        "client": inv.get("client_name") or "there",
        "studio": (trow["name"] if trow else "your photographer"),
        "title": inv["title"], "amount": inv["amount_display"],
        "pay_url": invoice_public_url(settings, inv["token"]),
        "note": f"{note}\n\n" if note else "",
    }
    msg = messaging.render(conn, tid, "invoice_send", ctx)
    notify(conn, settings, to=inv["client_email"], tenant_id=tid,
           subject=msg["subject"], body=msg["body"])


def run_recurring(conn: sqlite3.Connection, settings: Settings, *, limit: int = 500) -> int:
    """Across all tenants, generate the next invoice for every active profile due today or
    earlier. Returns the number generated.

    Each profile is its own committed unit, in this deliberate order:
      1. claim (atomic advance of next_run_at) + create the invoice, then **commit** — so
         the bill is durable before anything irreversible happens, and
      2. email the client their pay link (committed separately).
    This is what makes the money path safe: a crash or a failed SMTP send after step 1
    leaves exactly one committed invoice (the owner can resend the email) — it can never
    re-bill. Per-profile commit + try/except also isolates one bad profile so it can
    neither roll back nor re-bill the profiles already processed in the same sweep. A
    profile several periods behind advances one period per sweep until it catches up."""
    rows = conn.execute(
        "SELECT id FROM recurring_invoices WHERE active = 1 AND date(next_run_at) <= date('now') "
        "ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    generated = 0
    for r in rows:
        inv = prof = None
        try:
            if not _claim_due(conn, r["id"]):     # lost the race / no longer due
                conn.rollback()
                continue
            prof = dict(conn.execute(
                "SELECT * FROM recurring_invoices WHERE id = ?", (r["id"],)).fetchone())
            inv = _bill_profile(conn, settings, prof)
            conn.commit()                          # bill + claim durable BEFORE the email
            generated += 1
        except Exception:                          # noqa: BLE001 - isolate one bad profile
            conn.rollback()                        # undo just this profile; it retries next sweep
            continue
        try:
            _email_invoice(conn, settings, prof, inv)
            conn.commit()
        except Exception:                          # noqa: BLE001 - a send miss must not re-bill
            conn.rollback()                        # invoice already committed; only the email is lost
    return generated

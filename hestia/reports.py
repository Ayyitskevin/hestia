"""Finances reports — read-side aggregation that turns the A/R and expense data into
the two views a studio (and its accountant) actually asks for:

- **A/R aging:** outstanding invoices bucketed by how late they are, so the 90-days-
  overdue money stands out from what's merely not due yet.
- **Expense breakdown:** spend by category with its share of the total, so a studio
  sees where the money goes.

Pure reads over what the invoices/expenses modules already own; money is cents,
formatted for display with :func:`hestia.invoices.money`.
"""

from __future__ import annotations

import sqlite3

from .invoices import money

# (label, low, high) — high=None means open-ended. "Not yet due" is everything
# at or before its due date (days-overdue <= 0), including invoices with no due date.
_AGING_BUCKETS = (
    ("Not yet due", None, 0),
    ("1–30 days", 1, 30),
    ("31–60 days", 31, 60),
    ("61–90 days", 61, 90),
    ("90+ days", 91, None),
)


def ar_aging(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Outstanding (sent, standalone) invoices bucketed by days overdue. Plan
    installments are excluded — they're tracked under their payment plan, matching
    :func:`hestia.invoices.accounts_receivable`."""
    rows = conn.execute(
        "SELECT amount_cents, "
        # a parseable past due_date yields its age in days; empty/free-text or future
        # dates collapse to 0 ("not yet due")
        "  CASE WHEN date(due_date) IS NULL THEN 0 "
        "       ELSE CAST(julianday('now') - julianday(date(due_date)) AS INTEGER) END AS overdue_days "
        "FROM invoices WHERE tenant_id = ? AND status = 'sent' AND plan_id IS NULL",
        (tenant_id,),
    ).fetchall()

    buckets = [{"label": label, "low": low, "high": high, "cents": 0, "count": 0}
               for (label, low, high) in _AGING_BUCKETS]
    for r in rows:
        od = int(r["overdue_days"])
        for b in buckets:
            lo_ok = b["low"] is None or od >= b["low"]
            hi_ok = b["high"] is None or od <= b["high"]
            if lo_ok and hi_ok:
                b["cents"] += int(r["amount_cents"])
                b["count"] += 1
                break
    for b in buckets:
        b["display"] = money(b["cents"])
    total = sum(b["cents"] for b in buckets)
    overdue = sum(b["cents"] for b in buckets if b["label"] != "Not yet due")
    return {"buckets": buckets, "total_cents": total, "total": money(total),
            "overdue_cents": overdue, "overdue": money(overdue)}


def expense_breakdown(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Expenses grouped by category, biggest first, each with its share of the total."""
    rows = conn.execute(
        "SELECT category, COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total "
        "FROM expenses WHERE tenant_id = ? GROUP BY category ORDER BY total DESC, category",
        (tenant_id,),
    ).fetchall()
    out = [{"category": r["category"], "count": int(r["n"]), "cents": int(r["total"]),
            "display": money(int(r["total"]))} for r in rows]
    total = sum(o["cents"] for o in out)
    for o in out:
        o["pct"] = round(100 * o["cents"] / total) if total else 0
    return {"rows": out, "total_cents": total, "total": money(total)}

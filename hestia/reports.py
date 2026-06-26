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

import datetime
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


def _recent_months(n: int) -> list[str]:
    """The last ``n`` calendar months as 'YYYY-MM', oldest first, ending this month."""
    today = datetime.date.today()
    out, y, m = [], today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    out.reverse()
    return out


def monthly_pnl(conn: sqlite3.Connection, tenant_id: str, *, months: int = 6) -> list[dict]:
    """Revenue, expenses, and profit per calendar month for the last ``months``,
    oldest first. Revenue counts each sale once — paid invoices excluding the ones
    that back an order, plus paid orders — attributed to the month it was paid (the
    backing invoice's pay date for an order). Expenses use their incurred date when
    parseable, else when they were logged."""
    rev: dict[str, int] = {}
    for r in conn.execute(
        "SELECT strftime('%Y-%m', COALESCE(paid_at, created_at)) AS ym, amount_cents AS cents "
        "FROM invoices WHERE tenant_id = ? AND status = 'paid' AND id NOT IN "
        "(SELECT invoice_id FROM orders WHERE tenant_id = ? AND invoice_id IS NOT NULL)",
        (tenant_id, tenant_id)).fetchall():
        if r["ym"]:
            rev[r["ym"]] = rev.get(r["ym"], 0) + int(r["cents"])
    for r in conn.execute(
        "SELECT strftime('%Y-%m', COALESCE(i.paid_at, o.created_at)) AS ym, o.amount_cents AS cents "
        "FROM orders o LEFT JOIN invoices i ON i.id = o.invoice_id AND i.tenant_id = o.tenant_id "
        "WHERE o.tenant_id = ? AND o.status = 'paid'",
        (tenant_id,)).fetchall():
        if r["ym"]:
            rev[r["ym"]] = rev.get(r["ym"], 0) + int(r["cents"])
    exp: dict[str, int] = {}
    for r in conn.execute(
        "SELECT strftime('%Y-%m', COALESCE(date(incurred_on), created_at)) AS ym, "
        "SUM(amount_cents) AS cents FROM expenses WHERE tenant_id = ? GROUP BY ym",
        (tenant_id,)).fetchall():
        if r["ym"]:
            exp[r["ym"]] = exp.get(r["ym"], 0) + int(r["cents"])
    out = []
    for ym in _recent_months(months):
        rc, ec = rev.get(ym, 0), exp.get(ym, 0)
        out.append({"month": ym, "revenue_cents": rc, "expenses_cents": ec, "profit_cents": rc - ec,
                    "revenue": money(rc), "expenses": money(ec), "profit": money(rc - ec)})
    return out


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

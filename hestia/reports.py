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


def tax_collected(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Total sales tax collected on paid invoices — the liability a studio sets aside
    to remit. Tax rides on the invoice (an order's tax is on its backing invoice), so
    summing paid invoices counts each taxed sale once."""
    row = conn.execute(
        "SELECT COALESCE(SUM(tax_cents), 0) AS total FROM invoices "
        "WHERE tenant_id = ? AND status = 'paid'",
        (tenant_id,),
    ).fetchone()
    cents = int(row["total"])
    return {"cents": cents, "display": money(cents)}


def tax_by_period(conn: sqlite3.Connection, tenant_id: str, *, months: int = 12) -> dict:
    """Sales tax collected per calendar month (attributed to the month a taxed invoice was
    paid), for the last ``months`` oldest-first — the period breakdown an accountant files
    from, where :func:`tax_collected` gives only the lifetime total. Tax rides on the
    invoice (an order's tax is on its backing invoice), so summing paid invoices counts
    each taxed sale once."""
    by: dict[str, int] = {}
    for r in conn.execute(
        "SELECT strftime('%Y-%m', COALESCE(paid_at, created_at)) AS ym, "
        "       COALESCE(SUM(tax_cents), 0) AS cents "
        "FROM invoices WHERE tenant_id = ? AND status = 'paid' AND tax_cents > 0 GROUP BY ym",
        (tenant_id,),
    ).fetchall():
        if r["ym"]:
            by[r["ym"]] = int(r["cents"])
    rows = [{"month": ym, "cents": by.get(ym, 0), "display": money(by.get(ym, 0))}
            for ym in _recent_months(months)]
    total = sum(o["cents"] for o in rows)
    return {"rows": rows, "total_cents": total, "total": money(total)}


# project lifecycle stages in order; a project's status is its furthest point, so a later
# stage implies it passed the earlier ones
_FUNNEL_STAGES = ("lead", "booked", "shooting", "delivered", "archived")


def booking_funnel(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Lead → booked → delivered funnel from current project status, with conversion rates.
    Because status is the project's *furthest* stage, 'booked' counts everything past lead
    (booked/shooting/delivered/archived) and 'delivered' counts delivered + archived."""
    counts = dict.fromkeys(_FUNNEL_STAGES, 0)
    for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM projects WHERE tenant_id = ? GROUP BY status",
        (tenant_id,),
    ).fetchall():
        if r["status"] in counts:
            counts[r["status"]] = int(r["n"])
    total = sum(counts.values())
    booked = counts["booked"] + counts["shooting"] + counts["delivered"] + counts["archived"]
    delivered = counts["delivered"] + counts["archived"]
    return {
        "total": total, "booked": booked, "delivered": delivered, "by_status": counts,
        "lead_to_booked_pct": round(100 * booked / total) if total else 0,
        "booked_to_delivered_pct": round(100 * delivered / booked) if booked else 0,
        "overall_pct": round(100 * delivered / total) if total else 0,
    }


def top_clients(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 10) -> dict:
    """The studio's highest-value clients by collected (paid) revenue — for retention/VIP
    focus. Reuses the same lifetime figure as the client list (sum of paid invoice subtotals,
    tax excluded, matching the revenue elsewhere). Returns the top ``limit`` high-to-low plus
    the count of revenue clients and their combined total. Tenant-scoped, read-only."""
    from .crm import list_clients  # lazy: keeps the reports↔crm edge one-way

    earners = [c for c in list_clients(conn, tenant_id) if int(c["lifetime_cents"]) > 0]
    rows = [{"id": c["id"], "name": c["name"], "projects": int(c["project_count"]),
             "lifetime_cents": int(c["lifetime_cents"]), "lifetime": c["lifetime_display"]}
            for c in earners[:limit]]
    total = sum(int(c["lifetime_cents"]) for c in earners)
    return {"rows": rows, "count": len(earners), "total_cents": total, "total": money(total)}


def gallery_sales(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Per published gallery: views, client favorites, and paid print/album orders with
    revenue — so the studio sees which deliveries actually convert to sales. Plus the
    overall conversion (published galleries with at least one paid order) and total print
    revenue. Only paid orders count toward revenue/conversion; the gallery↔order join is
    tenant-matched. Sorted by revenue. Read-only, tenant-scoped."""
    rows = conn.execute(
        "SELECT g.id, g.title, COALESCE(g.view_count, 0) AS views, "
        "  (SELECT COUNT(*) FROM image_favorites f "
        "   WHERE f.gallery_id = g.id AND f.tenant_id = g.tenant_id) AS favorites, "
        "  COALESCE(SUM(CASE WHEN o.status = 'paid' THEN 1 ELSE 0 END), 0) AS orders, "
        "  COALESCE(SUM(CASE WHEN o.status = 'paid' THEN o.amount_cents ELSE 0 END), 0) AS revenue "
        "FROM galleries g "
        "LEFT JOIN orders o ON o.gallery_id = g.id AND o.tenant_id = g.tenant_id "
        "WHERE g.tenant_id = ? AND g.status = 'published' "
        "GROUP BY g.id ORDER BY revenue DESC, g.id",
        (tenant_id,),
    ).fetchall()
    out = [{"id": r["id"], "title": r["title"], "views": int(r["views"]),
            "favorites": int(r["favorites"]), "orders": int(r["orders"]),
            "revenue_cents": int(r["revenue"]), "revenue": money(int(r["revenue"]))}
           for r in rows]
    total_galleries = len(out)
    converted = sum(1 for g in out if g["orders"] > 0)
    total_revenue = sum(g["revenue_cents"] for g in out)
    return {
        "rows": out, "total_galleries": total_galleries, "converted": converted,
        "conversion_pct": round(100 * converted / total_galleries) if total_galleries else 0,
        "total_revenue_cents": total_revenue, "total_revenue": money(total_revenue),
    }


def lead_sources(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Leads grouped by how they heard about the studio, with how many of each went on to
    book — so the owner sees which channels actually convert. Projects with no recorded
    source (manually added, or pre-dating the field) report as 'Unknown'. 'Booked' counts
    any project past the lead stage; sorted by lead volume."""
    rows = conn.execute(
        "SELECT CASE WHEN TRIM(COALESCE(lead_source, '')) = '' THEN 'Unknown' "
        "            ELSE lead_source END AS source, "
        "       COUNT(*) AS leads, "
        "       COALESCE(SUM(CASE WHEN status IN ('booked','shooting','delivered','archived') "
        "                         THEN 1 ELSE 0 END), 0) AS booked "
        "FROM projects WHERE tenant_id = ? GROUP BY source ORDER BY leads DESC, source",
        (tenant_id,),
    ).fetchall()
    out = [{"source": r["source"], "leads": int(r["leads"]), "booked": int(r["booked"]),
            "pct": round(100 * int(r["booked"]) / int(r["leads"])) if r["leads"] else 0}
           for r in rows]
    return {"rows": out, "total_leads": sum(o["leads"] for o in out),
            "total_booked": sum(o["booked"] for o in out)}


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

"""Studio finances — the Plutus side: track expenses and see real profit.

Hestia already records revenue (paid invoices and paid orders); this adds the other
half of the ledger so a studio knows what a shoot actually netted, not just what it
billed. Expenses can be tagged to a project for per-job P&L. Money is cents
throughout; format for display with :func:`hestia.invoices.money`.
"""

from __future__ import annotations

import sqlite3

from .invoices import money

EXPENSE_CATEGORIES = (
    "second_shooter", "gear", "travel", "software", "albums_prints",
    "props_styling", "marketing", "other",
)


def create_expense(conn: sqlite3.Connection, *, tenant_id: str, amount_cents: int,
                   category: str = "other", description: str = "",
                   project_id: int | None = None, incurred_on: str = "") -> dict | None:
    cat = category if category in EXPENSE_CATEGORIES else "other"
    cur = conn.execute(
        "INSERT INTO expenses (tenant_id, project_id, category, description, amount_cents, incurred_on) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, project_id, cat, description.strip(), max(0, int(amount_cents)), incurred_on.strip()),
    )
    return get_expense(conn, tenant_id, cur.lastrowid)


def get_expense(conn: sqlite3.Connection, tenant_id: str, expense_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM expenses WHERE id = ? AND tenant_id = ?", (expense_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def list_expenses(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None,
                  limit: int = 200) -> list[dict]:
    base = ("SELECT e.*, p.name AS project_name FROM expenses e "
            "LEFT JOIN projects p ON p.id = e.project_id WHERE e.tenant_id = ? ")
    params: list = [tenant_id]
    if project_id is not None:
        base += "AND e.project_id = ? "
        params.append(project_id)
    base += "ORDER BY e.id DESC LIMIT ?"
    params.append(limit)
    out = [dict(r) for r in conn.execute(base, params).fetchall()]
    for e in out:
        e["amount_display"] = money(e["amount_cents"])
    return out


def delete_expense(conn: sqlite3.Connection, tenant_id: str, expense_id: int) -> bool:
    cur = conn.execute("DELETE FROM expenses WHERE id = ? AND tenant_id = ?", (expense_id, tenant_id))
    return cur.rowcount > 0


def _scalar(conn: sqlite3.Connection, sql: str, params) -> int:
    return int(conn.execute(sql, params).fetchone()["total"])


def expenses_total(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None) -> int:
    if project_id is None:
        return _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM expenses "
                       "WHERE tenant_id = ?", (tenant_id,))
    return _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM expenses "
                   "WHERE tenant_id = ? AND project_id = ?", (tenant_id, project_id))


def revenue_total(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None) -> int:
    """Collected revenue (cents). Tenant-wide counts paid invoices + paid orders;
    a single project counts its paid invoices (orders are gallery sales, not always
    project-tagged)."""
    if project_id is None:
        inv = _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM invoices "
                      "WHERE tenant_id = ? AND status = 'paid'", (tenant_id,))
        orders = _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM orders "
                         "WHERE tenant_id = ? AND status = 'paid'", (tenant_id,))
        return inv + orders
    return _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM invoices "
                   "WHERE tenant_id = ? AND project_id = ? AND status = 'paid'", (tenant_id, project_id))


def profit_summary(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None) -> dict:
    rev = revenue_total(conn, tenant_id, project_id=project_id)
    exp = expenses_total(conn, tenant_id, project_id=project_id)
    profit = rev - exp
    return {
        "revenue_cents": rev, "expenses_cents": exp, "profit_cents": profit,
        "revenue": money(rev), "expenses": money(exp), "profit": money(profit),
        "margin": round(100 * profit / rev) if rev else 0,
    }


def project_pnl(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 50) -> list[dict]:
    """Per-project P&L (invoiced revenue minus tagged expenses) for projects with any
    activity, lowest-profit first so a money-losing shoot surfaces at the top."""
    rows = conn.execute(
        "SELECT p.id, p.name, "
        "  COALESCE((SELECT SUM(amount_cents) FROM invoices i "
        "            WHERE i.project_id = p.id AND i.status = 'paid'), 0) AS revenue, "
        "  COALESCE((SELECT SUM(amount_cents) FROM expenses e WHERE e.project_id = p.id), 0) AS expenses "
        "FROM projects p WHERE p.tenant_id = ?",
        (tenant_id,),
    ).fetchall()
    out = []
    for r in rows:
        rev, exp = int(r["revenue"]), int(r["expenses"])
        if rev == 0 and exp == 0:
            continue
        out.append({"id": r["id"], "name": r["name"], "revenue": money(rev),
                    "expenses": money(exp), "profit": money(rev - exp), "profit_cents": rev - exp})
    out.sort(key=lambda x: x["profit_cents"])
    return out[:limit]

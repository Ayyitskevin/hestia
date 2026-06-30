"""Studio finances — the Plutus side: track expenses and see real profit.

Hestia already records revenue (paid invoices and paid orders); this adds the other
half of the ledger so a studio knows what a shoot actually netted, not just what it
billed. Expenses can be tagged to a project for per-job P&L. Money is cents
throughout; format for display with :func:`hestia.invoices.money`.
"""

from __future__ import annotations

import sqlite3

from .invoices import money
from .ownership import mask_invalid_project_id, owned_project_id

EXPENSE_CATEGORIES = (
    "second_shooter", "gear", "travel", "software", "albums_prints",
    "props_styling", "marketing", "other",
)


def create_expense(conn: sqlite3.Connection, *, tenant_id: str, amount_cents: int,
                   category: str = "other", description: str = "",
                   project_id: int | None = None, incurred_on: str = "") -> dict | None:
    cat = category if category in EXPENSE_CATEGORIES else "other"
    project_id = owned_project_id(conn, tenant_id, project_id)
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


def import_expenses(conn: sqlite3.Connection, *, tenant_id: str, rows: list[dict]) -> dict:
    """Bulk-create expenses from parsed rows (each: amount_cents, category, description,
    incurred_on). A row with no positive amount is skipped; a row that exactly matches an
    expense ALREADY recorded for this tenant (same date + amount + description) is skipped
    as a duplicate, so re-importing the same file is idempotent. The duplicate check is
    against the pre-existing rows only — so genuinely identical line items within a single
    file all import, but a second import of that file adds nothing. Category is normalized
    to a known one (else 'other'). Tenant-scoped. Returns a counts summary."""
    existing = {
        (r["d"], int(r["a"]), r["x"])
        for r in conn.execute(
            "SELECT TRIM(COALESCE(incurred_on, '')) AS d, amount_cents AS a, "
            "       TRIM(COALESCE(description, '')) AS x FROM expenses WHERE tenant_id = ?",
            (tenant_id,))
    }
    imported = skipped_duplicate = skipped_zero = 0
    for row in rows:
        cents = max(0, int(row.get("amount_cents") or 0))
        if cents <= 0:                                   # an expense needs an amount
            skipped_zero += 1
            continue
        desc = (row.get("description") or "").strip()
        incurred = (row.get("incurred_on") or "").strip()
        if (incurred, cents, desc) in existing:          # already recorded → idempotent skip
            skipped_duplicate += 1
            continue
        create_expense(conn, tenant_id=tenant_id, amount_cents=cents,
                       category=(row.get("category") or "other"), description=desc,
                       incurred_on=incurred)
        imported += 1
    return {"imported": imported, "skipped_duplicate": skipped_duplicate,
            "skipped_zero": skipped_zero}


def list_expenses(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None,
                  limit: int = 200) -> list[dict]:
    base = ("SELECT e.*, p.id AS valid_project_id, p.name AS project_name FROM expenses e "
            # tenant-match the join too, so a stray project_id can't surface a
            # different studio's project name
            "LEFT JOIN projects p ON p.id = e.project_id AND p.tenant_id = e.tenant_id "
            "WHERE e.tenant_id = ? ")
    params: list = [tenant_id]
    if project_id is not None:
        base += "AND p.id = ? "
        params.append(project_id)
    base += "ORDER BY e.id DESC LIMIT ?"
    params.append(limit)
    out = [mask_invalid_project_id(dict(r)) for r in conn.execute(base, params).fetchall()]
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
    return _scalar(conn, "SELECT COALESCE(SUM(e.amount_cents), 0) AS total FROM expenses e "
                   "JOIN projects p ON p.id = e.project_id AND p.tenant_id = e.tenant_id "
                   "WHERE e.tenant_id = ? AND p.id = ?", (tenant_id, project_id))


def revenue_total(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None) -> int:
    """Collected revenue (cents). Tenant-wide counts paid invoices + paid orders,
    a single project counts its paid invoices (orders are gallery sales, not always
    project-tagged).

    A gallery sale creates a *paired* invoice + order of the same amount (see
    ``orders.create_order``), and the pay flow marks both paid. Counting both would
    double the sale, so the tenant-wide invoice sum excludes order-backing invoices
    — each sale is then counted once, as its order row."""
    if project_id is None:
        inv = _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM invoices "
                      "WHERE tenant_id = ? AND status = 'paid' AND id NOT IN "
                      "(SELECT invoice_id FROM orders WHERE tenant_id = ? AND invoice_id IS NOT NULL)",
                      (tenant_id, tenant_id))
        orders = _scalar(conn, "SELECT COALESCE(SUM(amount_cents), 0) AS total FROM orders "
                         "WHERE tenant_id = ? AND status = 'paid'", (tenant_id,))
        return inv + orders
    return _scalar(conn, "SELECT COALESCE(SUM(i.amount_cents), 0) AS total FROM invoices i "
                   "JOIN projects p ON p.id = i.project_id AND p.tenant_id = i.tenant_id "
                   "WHERE i.tenant_id = ? AND p.id = ? AND i.status = 'paid' "
                   "AND (i.client_id IS NULL OR p.client_id = i.client_id)", (tenant_id, project_id))


def profit_summary(conn: sqlite3.Connection, tenant_id: str, *, project_id: int | None = None) -> dict:
    rev = revenue_total(conn, tenant_id, project_id=project_id)
    exp = expenses_total(conn, tenant_id, project_id=project_id)
    profit = rev - exp
    return {
        "revenue_cents": rev, "expenses_cents": exp, "profit_cents": profit,
        "revenue": money(rev), "expenses": money(exp), "profit": money(profit),
        "margin": round(100 * profit / rev) if rev else 0,
    }


def income_rows(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    """Collected income — paid invoices and paid orders — as flat rows, oldest first,
    for an accountant-ready export. Order-backing invoices are excluded so a gallery
    sale (its paired invoice + order) appears once, as its order row — matching the
    tenant-wide total in :func:`revenue_total`."""
    rows: list[dict] = []
    for r in conn.execute(
        "SELECT i.created_at AS date, i.title AS description, i.amount_cents, c.name AS client_name "
        "FROM invoices i LEFT JOIN clients c ON c.id = i.client_id AND c.tenant_id = i.tenant_id "
        "WHERE i.tenant_id = ? AND i.status = 'paid' AND i.id NOT IN "
        "(SELECT invoice_id FROM orders WHERE tenant_id = ? AND invoice_id IS NOT NULL)",
        (tenant_id, tenant_id)).fetchall():
        rows.append({"date": r["date"], "type": "invoice", "description": r["description"],
                     "client": r["client_name"] or "", "amount_cents": int(r["amount_cents"])})
    for r in conn.execute(
        "SELECT created_at AS date, name AS description, amount_cents FROM orders "
        "WHERE tenant_id = ? AND status = 'paid'", (tenant_id,)).fetchall():
        rows.append({"date": r["date"], "type": "order", "description": r["description"],
                     "client": "", "amount_cents": int(r["amount_cents"])})
    rows.sort(key=lambda x: x["date"])
    return rows


def project_pnl(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 50) -> list[dict]:
    """Per-project P&L (invoiced revenue minus tagged expenses) for projects with any
    activity, lowest-profit first so a money-losing shoot surfaces at the top."""
    rows = conn.execute(
        "SELECT p.id, p.name, "
        # tenant-scope the subqueries too: a stray project_id on another studio's
        # invoice/expense must never be attributed to this project's P&L.
        "  COALESCE((SELECT SUM(amount_cents) FROM invoices i "
        "            WHERE i.project_id = p.id AND i.tenant_id = p.tenant_id "
        "              AND i.status = 'paid' "
        "              AND (i.client_id IS NULL OR p.client_id = i.client_id)), 0) AS revenue, "
        "  COALESCE((SELECT SUM(amount_cents) FROM expenses e "
        "            WHERE e.project_id = p.id AND e.tenant_id = p.tenant_id), 0) AS expenses "
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

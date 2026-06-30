"""Payment plans — split a booking total into scheduled installments.

The canonical photographer flow is "deposit to book, balance by the event date,"
which is just a two-installment plan; this module supports any N installments.

Each installment is a real invoice (see :mod:`hestia.invoices`), so it gets its
own public pay link and the same idempotent settle path — a plan reuses the money
spine rather than inventing a parallel one. A plan's progress (paid / partial /
open) is *derived* from its child invoices, never stored, so it can never drift
from what was actually collected.
"""

from __future__ import annotations

import sqlite3

from .config import Settings
from .db import audit
from .invoices import create_invoice, money, tax_for
from .ownership import mask_invalid_project_id, normalize_client_project_ids

PLAN_STATUSES = ("active", "void")


def create_payment_plan(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant_id: str,
    title: str,
    installments: list[dict],
    client_id: int | None = None,
    project_id: int | None = None,
) -> dict:
    """Create a plan and one invoice per installment.

    ``installments`` is an ordered list of ``{"label", "amount_cents", "due_date"}``.
    The plan total is the sum of the installment amounts.
    """
    clean = [
        {
            "label": (i.get("label") or "Payment").strip(),
            "amount_cents": max(0, int(i.get("amount_cents", 0))),
            "due_date": (i.get("due_date") or "").strip(),
        }
        for i in installments
        if int(i.get("amount_cents", 0)) > 0
    ]
    client_id, project_id = normalize_client_project_ids(conn, tenant_id, client_id, project_id)
    total = sum(i["amount_cents"] for i in clean)
    cur = conn.execute(
        """
        INSERT INTO payment_plans (tenant_id, client_id, project_id, title, total_cents, currency)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (tenant_id, client_id, project_id, title.strip(), total, settings.currency),
    )
    plan_id = cur.lastrowid
    # apply the studio's sales tax to each installment (0 unless a rate is set), the
    # same additive way standalone invoices and orders do
    rate_row = conn.execute("SELECT tax_rate_bps FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    rate_bps = int(rate_row["tax_rate_bps"]) if rate_row else 0
    for seq, inst in enumerate(clean, start=1):
        create_invoice(
            conn, settings, tenant_id=tenant_id,
            title=f"{title.strip()} — {inst['label']}", amount_cents=inst["amount_cents"],
            client_id=client_id, project_id=project_id,
            plan_id=plan_id, due_date=inst["due_date"], sequence=seq,
            tax_cents=tax_for(inst["amount_cents"], rate_bps),
        )
    audit(conn, actor="owner", action="payment_plan.created", tenant_id=tenant_id,
          detail=f"{title.strip()} · {money(total, settings.currency)} · {len(clean)} installments")
    return get_payment_plan(conn, tenant_id, plan_id)


def installments_for_plan(conn: sqlite3.Connection, tenant_id: str, plan_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM invoices WHERE tenant_id = ? AND plan_id = ? ORDER BY sequence, id",
        (tenant_id, plan_id),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        cur = d.get("currency", "usd")
        tax = int(d.get("tax_cents") or 0)
        d["amount_display"] = money(d["amount_cents"], cur)       # pre-tax subtotal
        d["tax_cents"] = tax
        d["tax_display"] = money(tax, cur)
        d["total_cents"] = int(d["amount_cents"]) + tax
        d["total_display"] = money(d["total_cents"], cur)         # what the client pays
        out.append(d)
    return out


def _progress(total_cents: int, installments: list[dict]) -> dict:
    """Derive paid/remaining/status from the installment invoices."""
    paid = sum(i["amount_cents"] for i in installments if i["status"] == "paid")
    if installments and all(i["status"] == "paid" for i in installments):
        state = "paid"
    elif paid > 0:
        state = "partial"
    else:
        state = "open"
    return {"paid_cents": paid, "remaining_cents": max(0, total_cents - paid), "progress": state}


def get_payment_plan(conn: sqlite3.Connection, tenant_id: str, plan_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT pp.*, c.name AS client_name, c.email AS client_email,
               p.id AS valid_project_id, p.name AS project_name
          FROM payment_plans pp
          LEFT JOIN clients c ON c.id = pp.client_id AND c.tenant_id = pp.tenant_id
          LEFT JOIN projects p ON p.id = pp.project_id AND p.tenant_id = pp.tenant_id
           AND (pp.client_id IS NULL OR p.client_id = pp.client_id)
         WHERE pp.id = ? AND pp.tenant_id = ?
        """,
        (plan_id, tenant_id),
    ).fetchone()
    if not row:
        return None
    plan = mask_invalid_project_id(dict(row))
    plan["installments"] = installments_for_plan(conn, tenant_id, plan_id)
    plan.update(_progress(plan["total_cents"], plan["installments"]))
    plan["total_display"] = money(plan["total_cents"], plan.get("currency", "usd"))
    plan["paid_display"] = money(plan["paid_cents"], plan.get("currency", "usd"))
    plan["remaining_display"] = money(plan["remaining_cents"], plan.get("currency", "usd"))
    return plan


def list_payment_plans(
    conn: sqlite3.Connection, tenant_id: str, *,
    project_id: int | None = None, client_id: int | None = None,
) -> list[dict]:
    sql = (
        "SELECT pp.*, c.name AS client_name, p.id AS valid_project_id, p.name AS project_name "
        "  FROM payment_plans pp "
        "  LEFT JOIN clients c ON c.id = pp.client_id AND c.tenant_id = pp.tenant_id "
        "  LEFT JOIN projects p ON p.id = pp.project_id AND p.tenant_id = pp.tenant_id "
        "   AND (pp.client_id IS NULL OR p.client_id = pp.client_id) "
        " WHERE pp.tenant_id = ?"
    )
    params: list = [tenant_id]
    if project_id is not None:
        sql += " AND p.id = ?"
        params.append(project_id)
    if client_id is not None:
        sql += " AND pp.client_id = ?"
        params.append(client_id)
    sql += " ORDER BY pp.created_at DESC"
    plans = []
    for r in conn.execute(sql, params).fetchall():
        plan = mask_invalid_project_id(dict(r))
        installments = installments_for_plan(conn, tenant_id, plan["id"])
        plan.update(_progress(plan["total_cents"], installments))
        plan["total_display"] = money(plan["total_cents"], plan.get("currency", "usd"))
        plan["paid_display"] = money(plan["paid_cents"], plan.get("currency", "usd"))
        plan["remaining_display"] = money(plan["remaining_cents"], plan.get("currency", "usd"))
        plan["installment_count"] = len(installments)
        plans.append(plan)
    return plans


def void_payment_plan(conn: sqlite3.Connection, tenant_id: str, plan_id: int) -> None:
    """Void a plan and all of its still-unpaid installments. Paid installments
    are never touched — money already collected stands."""
    conn.execute(
        "UPDATE payment_plans SET status = 'void', updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (plan_id, tenant_id),
    )
    conn.execute(
        "UPDATE invoices SET status = 'void' "
        "WHERE tenant_id = ? AND plan_id = ? AND status != 'paid'",
        (tenant_id, plan_id),
    )
    audit(conn, actor="owner", action="payment_plan.void", tenant_id=tenant_id,
          detail=f"plan #{plan_id}")


def deposit_balance_installments(
    *, total_cents: int, deposit_cents: int, balance_due_date: str = "",
    deposit_label: str = "Deposit", balance_label: str = "Balance",
) -> list[dict]:
    """Build the two-installment deposit→balance schedule from a total + deposit.

    The deposit is clamped to the total; the balance is the remainder and is
    omitted when the deposit already covers everything (a paid-in-full plan).
    """
    total = max(0, int(total_cents))
    deposit = max(0, min(int(deposit_cents), total))
    out = [{"label": deposit_label, "amount_cents": deposit, "due_date": ""}]
    balance = total - deposit
    if balance > 0:
        out.append({"label": balance_label, "amount_cents": balance, "due_date": balance_due_date})
    return out

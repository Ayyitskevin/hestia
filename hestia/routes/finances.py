"""Studio finances — owner P&L: revenue (already tracked) minus expenses you add."""

from __future__ import annotations

import csv
import io
import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..auth import context_from_session
from ..crm import get_project, list_projects
from ..finances import (
    EXPENSE_CATEGORIES,
    create_expense,
    delete_expense,
    income_rows,
    list_expenses,
    profit_summary,
    project_pnl,
)
from ..reports import (
    ar_aging,
    booking_funnel,
    expense_breakdown,
    monthly_pnl,
    tax_by_period,
    tax_collected,
)
from .deps import db_conn, render

router = APIRouter()


def _csv_safe(value) -> str:
    """Neutralize CSV formula injection: a cell starting with = + - @ (or a control
    char) is treated as a formula by Excel/Sheets. Client names come from the public
    inquiry form, so an attacker-controlled value can reach the owner's export —
    prefix a quote so it stays literal text."""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows([[_csv_safe(cell) for cell in row] for row in rows])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _dollars(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.get("/finances")
def finances(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        summary = profit_summary(conn, tid)
        by_project = project_pnl(conn, tid)
        expenses = list_expenses(conn, tid)
        projects = list_projects(conn, tid)
    return render(request, "finances.html", auth=auth, summary=summary, by_project=by_project,
                  expenses=expenses, projects=projects, categories=EXPENSE_CATEGORIES)


@router.get("/finances/reports")
def finances_reports(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        aging = ar_aging(conn, tid)
        breakdown = expense_breakdown(conn, tid)
        trend = monthly_pnl(conn, tid)
        tax = tax_collected(conn, tid)
        tax_periods = tax_by_period(conn, tid)
        funnel = booking_funnel(conn, tid)
    return render(request, "finances_reports.html", auth=auth, aging=aging,
                  breakdown=breakdown, trend=trend, tax=tax, tax_periods=tax_periods,
                  funnel=funnel)


@router.post("/finances/expenses")
def add_expense(request: Request, amount: str = Form(""), category: str = Form("other"),
                description: str = Form(""), project_id: str = Form(""), incurred_on: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        try:
            dollars = float(amount)
            # 'inf'/'1e400'/'nan' parse as floats but round() to int overflows — treat
            # any non-finite amount as zero rather than letting it 500 the page.
            cents = round(dollars * 100) if math.isfinite(dollars) else 0
        except (TypeError, ValueError):
            cents = 0
        if cents > 0:
            raw = int(project_id) if project_id.strip().isdigit() else None
            # only tag a project this studio actually owns
            pid = raw if raw and get_project(conn, auth.tenant["id"], raw) else None
            create_expense(conn, tenant_id=auth.tenant["id"], amount_cents=cents,
                           category=category, description=description, project_id=pid,
                           incurred_on=incurred_on)
    return RedirectResponse("/finances", status_code=303)


@router.post("/finances/expenses/{expense_id}/delete")
def remove_expense(request: Request, expense_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_expense(conn, auth.tenant["id"], expense_id)
    return RedirectResponse("/finances", status_code=303)


@router.get("/finances/export/expenses.csv")
def export_expenses(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        expenses = list_expenses(conn, auth.tenant["id"], limit=100000)
    rows = [[e["incurred_on"] or e["created_at"], e["category"], e["description"],
             e.get("project_name") or "", _dollars(e["amount_cents"])] for e in expenses]
    return _csv_response("expenses.csv", ["date", "category", "description", "project", "amount"], rows)


@router.get("/finances/export/income.csv")
def export_income(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        income = income_rows(conn, auth.tenant["id"])
    rows = [[r["date"], r["type"], r["description"], r["client"], _dollars(r["amount_cents"])]
            for r in income]
    return _csv_response("income.csv", ["date", "type", "description", "client", "amount"], rows)


@router.get("/finances/export/tax.csv")
def export_tax(request: Request):
    """Sales tax collected per month — the file an accountant remits from."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        periods = tax_by_period(conn, auth.tenant["id"], months=24)
    rows = [[p["month"], _dollars(p["cents"])] for p in periods["rows"]]
    return _csv_response("tax-by-month.csv", ["month", "tax_collected"], rows)

"""Studio finances — owner P&L: revenue (already tracked) minus expenses you add."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..crm import list_projects
from ..finances import (
    EXPENSE_CATEGORIES,
    create_expense,
    delete_expense,
    list_expenses,
    profit_summary,
    project_pnl,
)
from .deps import db_conn, render

router = APIRouter()


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


@router.post("/finances/expenses")
def add_expense(request: Request, amount: str = Form(""), category: str = Form("other"),
                description: str = Form(""), project_id: str = Form(""), incurred_on: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        try:
            cents = round(float(amount) * 100)
        except (TypeError, ValueError):
            cents = 0
        if cents > 0:
            pid = int(project_id) if project_id.strip().isdigit() else None
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

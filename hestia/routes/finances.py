"""Studio finances — owner P&L: revenue (already tracked) minus expenses you add."""

from __future__ import annotations

import csv
import io
import math

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..crm import get_project, list_projects
from ..csv_export import csv_response
from ..db import audit
from ..finances import (
    EXPENSE_CATEGORIES,
    create_expense,
    delete_expense,
    import_expenses,
    income_rows,
    list_expenses,
    profit_summary,
    project_pnl,
)
from ..reports import (
    ar_aging,
    booking_funnel,
    expense_breakdown,
    gallery_sales,
    lead_sources,
    monthly_pnl,
    tax_by_period,
    tax_collected,
    top_clients,
)
from .deps import db_conn, render

router = APIRouter()


def _dollars(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    """Parse a money cell to a positive cent magnitude. Bank exports show expenses as
    negatives (debits); we take the magnitude either way. Overflow-safe (a huge finite
    value overflows to inf only after * 100, which round() can't convert)."""
    try:
        cents = float(str(raw).replace("$", "").replace(",", "").strip()) * 100
        return abs(int(round(cents))) if math.isfinite(cents) else 0
    except (ValueError, AttributeError, OverflowError):
        return 0


# header labels other tools / banks export, mapped to our fields
_EXPENSE_SYNONYMS = {
    "date": "date", "incurred_on": "date", "incurred": "date", "transaction date": "date",
    "posted": "date", "posted date": "date", "day": "date",
    "category": "category", "type": "category", "tag": "category",
    "description": "description", "desc": "description", "memo": "description",
    "note": "description", "notes": "description", "payee": "description", "merchant": "description",
    "amount": "amount", "amount_cents": "amount", "total": "amount", "debit": "amount",
    "cost": "amount", "price": "amount", "spent": "amount", "withdrawal": "amount",
    "charge": "amount", "outflow": "amount", "payment": "amount",
}

_MAX_IMPORT_BYTES = 5_000_000


def _parse_expense_csv(text: str) -> list[dict]:
    """Parse expense-import CSV text into row dicts. Row 0 is treated as a header only when it
    names an ``amount`` column (via synonyms, in any order) whose own cell is NOT a data amount
    — so a header with a numeric label elsewhere (a year/period column) still detects, while a
    headerless bank row (whose amount column holds a number) falls back to positional date,
    description, amount. Recognizes common bank labels (Transaction Date, Memo, Debit,
    Withdrawal…). Raises csv.Error on a malformed/binary file."""
    records = [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
    if not records:
        return []
    head = [c.strip().lower() for c in records[0]]
    head_map: dict[str, int] = {}
    for i, h in enumerate(head):
        field = _EXPENSE_SYNONYMS.get(h)
        if field and field not in head_map:      # first recognized synonym for a field wins
            head_map[field] = i
    # a header must map an amount column whose OWN cell isn't a real amount (else it's data)
    if "amount" in head_map and _to_cents(records[0][head_map["amount"]]) <= 0:
        field_map, data = head_map, records[1:]
    else:
        field_map, data = {"date": 0, "description": 1, "amount": 2}, records

    def cell(rec: list[str], field: str) -> str:
        i = field_map.get(field)
        return rec[i].strip() if i is not None and i < len(rec) else ""

    rows = []
    for rec in data:
        rows.append({"incurred_on": cell(rec, "date"), "category": cell(rec, "category"),
                     "description": cell(rec, "description"),
                     "amount_cents": _to_cents(cell(rec, "amount"))})
    return rows


@router.get("/finances/import")
def expenses_import_form(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
    return render(request, "finances_import.html", auth=auth, summary=None, error=None)


@router.post("/finances/import")
async def expenses_import(request: Request, file: UploadFile = File(...)):
    # authenticate BEFORE reading the body — a cookieless POST is CSRF-exempt, so reading
    # first would let an anonymous caller force an unbounded read
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        raw = await file.read()
        if len(raw) > _MAX_IMPORT_BYTES:
            return render(request, "finances_import.html", auth=auth, summary=None,
                          error=f"That file is too large (limit {_MAX_IMPORT_BYTES // 1_000_000} MB).")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        try:
            rows = _parse_expense_csv(text)
        except csv.Error:
            rows = None
        if rows is None:
            return render(request, "finances_import.html", auth=auth, summary=None,
                          error="That file didn't look like a CSV — please upload a .csv export.")
        summary = import_expenses(conn, tenant_id=auth.tenant["id"], rows=rows)
        if summary["imported"]:
            audit(conn, actor="owner", action="expenses.imported", tenant_id=auth.tenant["id"],
                  detail=(f"{summary['imported']} imported · {summary['skipped_duplicate']} dupes "
                          f"· {summary['skipped_zero']} no-amount"))
            conn.commit()
    return render(request, "finances_import.html", auth=auth, summary=summary, error=None)


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
        sources = lead_sources(conn, tid)
        galleries = gallery_sales(conn, tid)
        clients = top_clients(conn, tid)
    return render(request, "finances_reports.html", auth=auth, aging=aging,
                  breakdown=breakdown, trend=trend, tax=tax, tax_periods=tax_periods,
                  funnel=funnel, sources=sources, galleries=galleries, clients=clients)


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
    return csv_response("expenses.csv", ["date", "category", "description", "project", "amount"], rows)


@router.get("/finances/export/income.csv")
def export_income(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        income = income_rows(conn, auth.tenant["id"])
    rows = [[r["date"], r["type"], r["description"], r["client"], _dollars(r["amount_cents"])]
            for r in income]
    return csv_response("income.csv", ["date", "type", "description", "client", "amount"], rows)


@router.get("/finances/export/tax.csv")
def export_tax(request: Request):
    """Sales tax collected per month — the file an accountant remits from."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        periods = tax_by_period(conn, auth.tenant["id"], months=24)
    rows = [[p["month"], _dollars(p["cents"])] for p in periods["rows"]]
    return csv_response("tax-by-month.csv", ["month", "tax_collected"], rows)

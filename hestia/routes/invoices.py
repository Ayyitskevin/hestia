"""Invoice routes (studio side) — create, send, and track payment."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..crm import list_clients, list_projects
from ..db import audit
from ..email import notify
from ..invoices import (
    create_invoice,
    get_invoice,
    invoice_public_url,
    list_invoices,
    send_invoice,
    void_invoice,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/invoices")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    try:
        dollars = float(raw.replace("$", "").replace(",", "").strip())
        # 'inf'/'nan' parse but overflow round() to int — treat non-finite as zero.
        return int(round(dollars * 100)) if math.isfinite(dollars) else 0
    except (ValueError, AttributeError):
        return 0


@router.get("")
def invoices_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        # Plan installments live under their payment plan, not the flat list.
        invoices = list_invoices(conn, auth.tenant["id"], standalone_only=True)
    return render(request, "invoices/invoices.html", auth=auth, invoices=invoices)


@router.get("/new")
def invoice_new(request: Request, project_id: int | None = None, client_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "invoices/invoice_new.html", auth=auth, clients=clients,
                  projects=projects, preselect_project=project_id, preselect_client=client_id)


@router.post("")
def invoice_create(request: Request, title: str = Form(...), amount: str = Form("0"),
                   client_id: str = Form(""), project_id: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        invoice = create_invoice(
            conn, settings_of(request), tenant_id=auth.tenant["id"], title=title,
            amount_cents=_to_cents(amount),
            client_id=int(client_id) if client_id.strip().isdigit() else None,
            project_id=int(project_id) if project_id.strip().isdigit() else None,
        )
    return RedirectResponse(f"/invoices/{invoice['id']}", status_code=303)


@router.get("/{invoice_id}")
def invoice_detail(request: Request, invoice_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        invoice = get_invoice(conn, auth.tenant["id"], invoice_id)
        if not invoice:
            return RedirectResponse("/invoices", status_code=303)
    pay_url = invoice_public_url(settings_of(request), invoice["token"])
    return render(request, "invoices/invoice_detail.html", auth=auth, invoice=invoice, pay_url=pay_url)


@router.post("/{invoice_id}/send")
def invoice_send(request: Request, invoice_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        send_invoice(conn, auth.tenant["id"], invoice_id)
        invoice = get_invoice(conn, auth.tenant["id"], invoice_id)
        if invoice:
            audit(conn, actor="owner", action="invoice.sent", tenant_id=auth.tenant["id"],
                  detail=f"{invoice['title']} · {invoice['amount_display']}")
        # Email the client their pay link (mock records it; smtp also delivers).
        if invoice and invoice.get("client_email"):
            pay_url = invoice_public_url(settings, invoice["token"])
            studio = auth.tenant.get("name", "your photographer")
            notify(
                conn, settings, to=invoice["client_email"], tenant_id=auth.tenant["id"],
                subject=f"{studio}: invoice for {invoice['title']} ({invoice['amount_display']})",
                body=(f"Hi {invoice.get('client_name') or 'there'},\n\n"
                      f"{studio} sent you an invoice for {invoice['title']} — "
                      f"{invoice['amount_display']}.\n\nPay securely here:\n{pay_url}\n\n"
                      f"Thank you!"),
            )
        conn.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/{invoice_id}/void")
def invoice_void(request: Request, invoice_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        invoice = get_invoice(conn, auth.tenant["id"], invoice_id)
        void_invoice(conn, auth.tenant["id"], invoice_id)
        if invoice:
            audit(conn, actor="owner", action="invoice.void", tenant_id=auth.tenant["id"],
                  detail=invoice["title"])
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)

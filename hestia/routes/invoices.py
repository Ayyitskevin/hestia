"""Invoice routes (studio side) — create, send, and track payment."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import messaging
from ..auth import context_from_session
from ..crm import get_client, get_project, list_clients, list_projects
from ..db import audit
from ..email import notify
from ..invoices import (
    accounts_receivable,
    create_invoice,
    get_invoice,
    invoice_public_url,
    list_invoices,
    record_invoice_reminder,
    record_offline_payment,
    send_invoice,
    send_invoice_reminder,
    set_invoice_note,
    tax_for,
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
        ar = accounts_receivable(conn, auth.tenant["id"])
    return render(request, "invoices/invoices.html", auth=auth, invoices=invoices, ar=ar)


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
                   client_id: str = Form(""), project_id: str = Form(""), note: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        # only attach a client/project this studio actually owns — a stray cross-tenant
        # id would otherwise ride along on the invoice and surface via the joins
        raw_c = int(client_id) if client_id.strip().isdigit() else None
        cid = raw_c if raw_c and get_client(conn, tid, raw_c) else None
        raw_p = int(project_id) if project_id.strip().isdigit() else None
        pid = raw_p if raw_p and get_project(conn, tid, raw_p) else None
        subtotal = _to_cents(amount)
        # add the studio's sales tax (0 unless they've set a rate) on top of the subtotal
        tax = tax_for(subtotal, auth.tenant.get("tax_rate_bps") or 0)
        invoice = create_invoice(
            conn, settings_of(request), tenant_id=tid, title=title,
            amount_cents=subtotal, client_id=cid, project_id=pid, tax_cents=tax, note=note,
        )
    return RedirectResponse(f"/invoices/{invoice['id']}", status_code=303)


@router.post("/{invoice_id}/note")
def invoice_note(request: Request, invoice_id: int, note: str = Form("")):
    """Add or edit the invoice's personal note (thank-you, payment instructions)."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_invoice_note(conn, auth.tenant["id"], invoice_id, note)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


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
            note = (invoice.get("note") or "").strip()
            ctx = {
                "client": invoice.get("client_name") or "there",
                "studio": auth.tenant.get("name", "your photographer"),
                "title": invoice["title"], "amount": invoice["amount_display"],
                "pay_url": invoice_public_url(settings, invoice["token"]),
                "note": f"{note}\n\n" if note else "",      # the studio's personal message
            }
            msg = messaging.render(conn, auth.tenant["id"], "invoice_send", ctx)
            notify(conn, settings, to=invoice["client_email"], tenant_id=auth.tenant["id"],
                   subject=msg["subject"], body=msg["body"])
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


@router.post("/{invoice_id}/record-payment")
def invoice_record_payment(request: Request, invoice_id: int, method: str = Form("other")):
    """Mark an invoice paid for money taken outside the pay link (cash, check, transfer).
    Idempotent — a double submit settles once."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        record_offline_payment(conn, auth.tenant["id"], invoice_id, method=method)
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/{invoice_id}/receipt")
def invoice_receipt(request: Request, invoice_id: int):
    """Email the client a paid receipt — owner-initiated, for a settled invoice. Works
    however it was paid (online or recorded offline); a no-op if unpaid or no email."""
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        invoice = get_invoice(conn, auth.tenant["id"], invoice_id)
        to = (invoice.get("client_email") or "").strip() if invoice else ""
        if invoice and invoice["status"] == "paid" and to:
            ctx = {
                "client": invoice.get("client_name") or "there",
                "studio": auth.tenant.get("name", "your studio"),
                "title": invoice["title"],
                "amount": invoice.get("total_display") or invoice.get("amount_display"),
            }
            msg = messaging.render(conn, auth.tenant["id"], "invoice_receipt", ctx)
            notify(conn, settings, to=to, subject=msg["subject"], body=msg["body"],
                   tenant_id=auth.tenant["id"])
            audit(conn, actor="owner", action="invoice.receipt", tenant_id=auth.tenant["id"],
                  detail=invoice["title"])
            conn.commit()
    return RedirectResponse(f"/invoices/{invoice_id}", status_code=303)


@router.post("/{invoice_id}/remind")
def invoice_remind(request: Request, invoice_id: int):
    """Owner-initiated past-due nudge. Bypasses the auto-sweep cooldown (the owner
    explicitly clicked), but still only nudges a 'sent', unpaid invoice."""
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        invoice = get_invoice(conn, tid, invoice_id)
        if invoice and invoice["status"] == "sent":
            if send_invoice_reminder(conn, settings, invoice):
                record_invoice_reminder(conn, tid, invoice_id)
                audit(conn, actor="owner", action="invoice.reminded", tenant_id=tid,
                      detail=f"{invoice['title']} · {invoice['amount_display']}")
            else:
                # no client email on file — leave a trail rather than a silent no-op
                audit(conn, actor="owner", action="invoice.remind_skipped", tenant_id=tid,
                      detail=f"{invoice['title']} · no client email")
        conn.commit()
    return RedirectResponse("/invoices", status_code=303)

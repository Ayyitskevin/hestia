"""Public pay routes — the client-facing invoice checkout."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..invoices import get_invoice_by_token, mark_paid
from ..orders import fulfill_for_invoice_token
from ..payments import PaymentError, build_payments
from ..ratelimit import enforce
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of

router = APIRouter()


@router.get("/pay/{token}")
def pay_page(request: Request, token: str):
    with db_conn(request) as conn:
        invoice = get_invoice_by_token(conn, token)
        if not invoice or invoice["status"] == "void":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, invoice["tenant_id"])
    return render(request, "invoices/pay.html", auth=None, invoice=invoice, tenant=tenant)


@router.get("/pay/{token}/receipt")
def pay_receipt(request: Request, token: str):
    """A printable receipt the client can save — only for a paid invoice. Unpaid or
    void/unknown falls back to the pay page (which 404s for void/unknown)."""
    with db_conn(request) as conn:
        invoice = get_invoice_by_token(conn, token)
        if not invoice or invoice["status"] != "paid":
            return RedirectResponse(f"/pay/{token}", status_code=303)
        tenant = get_tenant(conn, invoice["tenant_id"])
        crow = conn.execute(
            "SELECT name FROM clients WHERE id = ? AND tenant_id = ?",
            (invoice.get("client_id"), invoice["tenant_id"]),
        ).fetchone() if invoice.get("client_id") else None
        client_name = crow["name"] if crow else ""
    return render(request, "invoices/receipt.html", auth=None, invoice=invoice,
                  tenant=tenant, client_name=client_name)


@router.post("/pay/{token}/checkout")
def pay_checkout(request: Request, token: str):
    enforce(request, "checkout")
    settings = settings_of(request)
    with db_conn(request) as conn:
        invoice = get_invoice_by_token(conn, token)
        if not invoice or invoice["status"] == "void":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        if invoice["status"] == "paid":
            return RedirectResponse(f"/pay/{token}", status_code=303)
        provider = build_payments(settings)
        success_url = f"{settings.public_url.rstrip('/')}/pay/{token}"
        try:
            result = provider.create_checkout(invoice, success_url=success_url)
        except PaymentError:
            return RedirectResponse(f"/pay/{token}", status_code=303)
        if result.paid_now:
            mark_paid(conn, token=token, provider=provider.backend, ref=result.ref)
            # If this invoice backs a print order, settle it to the fulfillment lab.
            fulfill_for_invoice_token(conn, token)
    return RedirectResponse(result.url, status_code=303)

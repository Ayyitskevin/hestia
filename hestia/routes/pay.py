"""Public pay routes — the client-facing invoice checkout."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..discounts import DiscountError, apply_code_to_invoice
from ..invoices import get_invoice_by_token, invoice_items, mark_paid
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
        items = invoice_items(conn, invoice["tenant_id"], invoice["id"])
    return render(request, "invoices/pay.html", auth=None, invoice=invoice, tenant=tenant, items=items)


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
        items = invoice_items(conn, invoice["tenant_id"], invoice["id"])
    return render(request, "invoices/receipt.html", auth=None, invoice=invoice,
                  tenant=tenant, client_name=client_name, items=items)


@router.post("/pay/{token}/discount")
def pay_apply_discount(request: Request, token: str, code: str = Form("")):
    """Client applies a promo code to their invoice before paying. Takes the write lock so
    concurrent applies serialize; on success the page reloads with the reduced total, on
    failure it re-renders with the reason."""
    enforce(request, "checkout")
    try:
        with db_conn(request) as conn:
            conn.execute("BEGIN IMMEDIATE")          # serialize: no over-redeem / double-apply
            result = apply_code_to_invoice(conn, invoice_token=token, code=code)
    except DiscountError:                            # anomaly after the claim → it rolled back
        result = {"ok": False, "error": "Sorry — we couldn't apply that code. Please try again."}
    if result.get("ok"):
        return RedirectResponse(f"/pay/{token}", status_code=303)
    with db_conn(request) as conn:
        invoice = get_invoice_by_token(conn, token)
        if not invoice or invoice["status"] == "void":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, invoice["tenant_id"])
        items = invoice_items(conn, invoice["tenant_id"], invoice["id"])
    return render(request, "invoices/pay.html", auth=None, invoice=invoice, tenant=tenant,
                  items=items, error=result.get("error"), status_code=400)


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
        # A fully-discounted (or otherwise $0) invoice has nothing to charge — settle it
        # directly, since payment providers reject a zero / below-minimum amount.
        if int(invoice.get("total_cents") or 0) <= 0:
            mark_paid(conn, token=token, provider="comp", ref="zero_total")
            fulfill_for_invoice_token(conn, token)
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

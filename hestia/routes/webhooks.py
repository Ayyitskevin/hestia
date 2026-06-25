"""Inbound webhooks — Stripe checkout confirmation completes the money path.

When a client pays via Stripe Checkout, Stripe POSTs ``checkout.session.completed``
here. We verify the signature, pull the invoice token we stamped on the session
(``client_reference_id`` / ``metadata.invoice_token``), and idempotently mark the
invoice paid. This is the piece that makes ``payments_backend=stripe`` real.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..invoices import mark_paid
from ..payments import checkout_token_from_event, verify_stripe_signature
from .deps import db_conn, settings_of

router = APIRouter()


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    settings = settings_of(request)
    if not settings.stripe_webhook_secret:
        return JSONResponse({"error": "stripe webhook not configured"}, status_code=503)

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    if not verify_stripe_signature(payload, sig, settings.stripe_webhook_secret):
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    token = checkout_token_from_event(payload)
    paid = False
    if token:
        with db_conn(request) as conn:
            paid = mark_paid(conn, token=token, provider="stripe", ref="stripe_checkout")
    return {"received": True, "paid": paid}

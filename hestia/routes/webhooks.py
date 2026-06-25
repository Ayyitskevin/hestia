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
from ..subscriptions import apply_plan, canceled_tenant_from_event, subscription_from_event
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

    result = {"received": True, "paid": False, "subscription": None}
    token = checkout_token_from_event(payload)         # invoice payment
    sub = subscription_from_event(payload)             # studio subscription started
    canceled = canceled_tenant_from_event(payload)     # studio subscription canceled
    with db_conn(request) as conn:
        if token:
            result["paid"] = mark_paid(conn, token=token, provider="stripe", ref="stripe_checkout")
        if sub:
            tenant_id, plan, ref = sub
            apply_plan(conn, tenant_id, plan=plan, provider="stripe", provider_ref=ref)
            result["subscription"] = f"{plan}:active"
        elif canceled:
            apply_plan(conn, canceled, plan="beta", status="canceled", provider="stripe")
            result["subscription"] = "beta:canceled"
    return result

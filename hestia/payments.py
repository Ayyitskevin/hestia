"""Payments seam — checkout for invoices (essence of Plutus checkout + Mise invoices).

A pluggable provider, same shape as the vision and storage seams:

- ``mock``   — simulates a successful checkout with no keys. The default, so the
  whole invoice → pay → paid flow is testable in CI and demos.
- ``stripe`` — creates a real Stripe Checkout Session (test or live keys). The
  call is implemented; a webhook marks the invoice paid (Phase: wire the webhook).

Honesty: ``mock`` clearly labels payments as simulated; nothing is charged.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from .config import Settings


class PaymentError(RuntimeError):
    pass


# ── Stripe webhook verification (completes the money path) ──────────────────


def verify_stripe_signature(
    payload: bytes, sig_header: str, secret: str, *, tolerance: int = 300, now: float | None = None
) -> bool:
    """Verify a Stripe-Signature header (``t=<ts>,v1=<sig>``) against the payload.

    Same scheme Stripe's SDK uses: HMAC-SHA256 of ``"{t}.{payload}"`` keyed by the
    endpoint secret. Constant-time compare; optional replay-window check.
    """
    if not secret or not sig_header:
        return False
    parts = dict(
        p.split("=", 1) for p in sig_header.split(",") if "=" in p
    )
    timestamp = parts.get("t")
    sent = parts.get("v1")
    if not timestamp or not sent:
        return False
    signed = f"{timestamp}.{payload.decode('utf-8', 'replace')}".encode()
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sent):
        return False
    if tolerance > 0:
        current = now if now is not None else time.time()
        try:
            if abs(current - int(timestamp)) > tolerance:
                return False
        except ValueError:
            return False
    return True


def stripe_signature_header(payload: bytes, secret: str, *, timestamp: int) -> str:
    """Build a valid Stripe-Signature header (used by tests and tooling)."""
    signed = f"{timestamp}.{payload.decode('utf-8', 'replace')}".encode()
    sig = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={sig}"


def checkout_token_from_event(payload: bytes) -> str | None:
    """Extract the invoice token from a ``checkout.session.completed`` event."""
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    if event.get("type") != "checkout.session.completed":
        return None
    obj = (event.get("data") or {}).get("object") or {}
    return obj.get("client_reference_id") or (obj.get("metadata") or {}).get("invoice_token")


@dataclass
class CheckoutResult:
    url: str           # where to send the payer
    ref: str = ""      # provider session/reference id
    paid_now: bool = False  # mock settles immediately; stripe settles via webhook
    simulated: bool = False


class MockPayments:
    backend = "mock"

    def create_checkout(self, invoice: dict, *, success_url: str, cancel_url: str = "/") -> CheckoutResult:
        # No external call; the payer "completes" instantly and returns to the page.
        return CheckoutResult(url=success_url, ref=f"mock_{invoice['token']}",
                              paid_now=True, simulated=True)


class StripePayments:
    backend = "stripe"

    def __init__(self, settings: Settings):
        self.settings = settings

    def create_checkout(self, invoice: dict, *, success_url: str, cancel_url: str = "/") -> CheckoutResult:
        import httpx

        if not self.settings.stripe_secret_key:
            raise PaymentError("HESTIA_STRIPE_SECRET_KEY not set for stripe backend")
        # Stripe Checkout Session via the REST API (form-encoded), mirroring Plutus.
        data = {
            "mode": "payment",
            "success_url": success_url,
            "cancel_url": cancel_url,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": invoice["currency"],
            "line_items[0][price_data][unit_amount]": str(invoice["amount_cents"]),
            "line_items[0][price_data][product_data][name]": invoice["title"],
            "client_reference_id": invoice["token"],
            "metadata[invoice_token]": invoice["token"],
        }
        try:
            resp = httpx.post("https://api.stripe.com/v1/checkout/sessions", data=data,
                              auth=(self.settings.stripe_secret_key, ""), timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise PaymentError(f"stripe checkout failed: {exc}") from exc
        # paid_now=False: Stripe confirms asynchronously via webhook.
        return CheckoutResult(url=body["url"], ref=body.get("id", ""), paid_now=False)


def build_payments(settings: Settings):
    if settings.payments_backend == "stripe":
        return StripePayments(settings)
    return MockPayments()

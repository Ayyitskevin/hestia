"""Payments seam — checkout for invoices (essence of Plutus checkout + Mise invoices).

A pluggable provider, same shape as the vision and storage seams:

- ``mock``   — simulates a successful checkout with no keys. The default, so the
  whole invoice → pay → paid flow is testable in CI and demos.
- ``stripe`` — creates a real Stripe Checkout Session (test or live keys). The
  call is implemented; a webhook marks the invoice paid (Phase: wire the webhook).

Honesty: ``mock`` clearly labels payments as simulated; nothing is charged.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


class PaymentError(RuntimeError):
    pass


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

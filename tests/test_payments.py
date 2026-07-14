"""Payments seam — provider selection and behavior."""

import dataclasses

import pytest

from hestia.payments import (
    MockPayments,
    PaymentError,
    StripePayments,
    build_payments,
    charge_cents,
)


def test_build_payments_defaults_to_mock(settings):
    assert isinstance(build_payments(settings), MockPayments)


def test_build_payments_stripe_selected(settings):
    s = dataclasses.replace(settings, payments_backend="stripe")
    assert isinstance(build_payments(s), StripePayments)


def test_mock_checkout_settles_immediately():
    inv = {"token": "tok123", "amount_cents": 5000, "currency": "usd", "title": "X"}
    result = MockPayments().create_checkout(inv, success_url="/pay/tok123")
    assert result.paid_now is True
    assert result.simulated is True
    assert result.url == "/pay/tok123"


def test_stripe_without_key_raises(settings):
    s = dataclasses.replace(settings, payments_backend="stripe", stripe_secret_key="")
    inv = {"token": "t", "amount_cents": 100, "currency": "usd", "title": "X"}
    with pytest.raises(PaymentError):
        StripePayments(s).create_checkout(inv, success_url="/pay/t")


def test_charge_cents_uses_hydrated_due_and_never_goes_negative():
    assert charge_cents({"amount_cents": 10000, "tax_cents": 800}) == 10800
    assert charge_cents({"amount_cents": 10000, "amount_due_cents": 2500}) == 2500
    assert charge_cents({"amount_cents": 10000, "amount_due_cents": -1}) == 0


def test_stripe_checkout_sends_server_authoritative_amount(settings, monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"url": "https://checkout.stripe.test/session", "id": "cs_test_123"}

    def post(url, *, data, auth, timeout):
        captured.update(url=url, data=data, auth=auth, timeout=timeout)
        return Response()

    monkeypatch.setattr("httpx.post", post)
    configured = dataclasses.replace(settings, stripe_secret_key="sk_test_private")
    invoice = {
        "token": "invoice-token",
        "title": "Wedding balance",
        "currency": "usd",
        "amount_cents": 10000,
        "tax_cents": 800,
        "amount_due_cents": 2500,
    }

    result = StripePayments(configured).create_checkout(
        invoice,
        success_url="https://hestia.test/pay/invoice-token",
        cancel_url="https://hestia.test/pay/invoice-token?canceled=1",
    )

    assert result.url == "https://checkout.stripe.test/session"
    assert result.ref == "cs_test_123" and result.paid_now is False
    assert captured["url"] == "https://api.stripe.com/v1/checkout/sessions"
    assert captured["auth"] == ("sk_test_private", "") and captured["timeout"] == 30
    assert captured["data"]["line_items[0][price_data][unit_amount]"] == "2500"
    assert captured["data"]["client_reference_id"] == "invoice-token"


def test_stripe_checkout_wraps_provider_failure(settings, monkeypatch):
    def post(*args, **kwargs):
        raise TimeoutError("provider timed out")

    monkeypatch.setattr("httpx.post", post)
    configured = dataclasses.replace(settings, stripe_secret_key="sk_test_private")
    invoice = {"token": "t", "amount_cents": 100, "currency": "usd", "title": "X"}

    with pytest.raises(PaymentError, match="stripe checkout failed: provider timed out"):
        StripePayments(configured).create_checkout(invoice, success_url="/pay/t")

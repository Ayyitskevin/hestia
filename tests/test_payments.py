"""Payments seam — provider selection and behavior."""

import dataclasses

import pytest

from hestia.payments import MockPayments, PaymentError, StripePayments, build_payments


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

"""Studio subscriptions — seam selection, plan changes, Stripe webhook activation."""

import dataclasses
import json
import time

import pytest
from conftest import CSRFClient, login_owner, onboard_studio
from fastapi.testclient import TestClient

from hestia.db import connect
from hestia.main import create_app
from hestia.payments import stripe_signature_header
from hestia.subscriptions import (
    MockSubscriptions,
    PortalResult,
    StripeSubscriptions,
    SubscribeResult,
    SubscriptionError,
    apply_plan,
    build_subscriptions,
    canceled_tenant_from_event,
    get_subscription,
    subscription_from_event,
)
from hestia.tenants import create_tenant

# ── seam + data access ──────────────────────────────────────────────────────


def test_build_subscriptions_selection(settings):
    assert isinstance(build_subscriptions(settings), MockSubscriptions)
    assert isinstance(build_subscriptions(dataclasses.replace(settings, subscription_backend="stripe")),
                      StripeSubscriptions)


def test_mock_subscribe_activates_now(settings):
    r = MockSubscriptions().subscribe(tenant={"id": "t1"}, plan="studio", success_url="/x")
    assert isinstance(r, SubscribeResult) and r.activated and r.simulated


def test_mock_portal_returns_return_url(settings):
    r = MockSubscriptions().portal(
        tenant={"id": "t1"},
        subscription={"provider_ref": ""},
        return_url="/settings/account",
    )
    assert isinstance(r, PortalResult)
    assert r.url == "/settings/account" and r.simulated


def test_stripe_subscribe_needs_key(settings):
    s = dataclasses.replace(settings, subscription_backend="stripe", stripe_secret_key="")
    with pytest.raises(SubscriptionError):
        StripeSubscriptions(s).subscribe(tenant={"id": "t1"}, plan="studio", success_url="/x")


def test_stripe_subscribe_uses_flat_40_trial(monkeypatch, settings):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"url": "https://checkout.stripe.test/session"}

    def fake_post(url, *, data, auth, timeout):
        captured.update({"url": url, "data": data, "auth": auth, "timeout": timeout})
        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    s = dataclasses.replace(settings, subscription_backend="stripe", stripe_secret_key="sk_test")
    r = StripeSubscriptions(s).subscribe(
        tenant={"id": "t1", "owner_email": "owner@example.com"},
        plan="studio",
        success_url="https://app.test/settings/billing",
    )
    assert r.url.startswith("https://checkout")
    assert captured["auth"] == ("sk_test", "")
    data = captured["data"]
    assert data["mode"] == "subscription"
    assert data["line_items[0][price_data][unit_amount]"] == "4000"
    assert data["line_items[0][price_data][recurring][interval]"] == "month"
    assert data["line_items[0][price_data][product_data][name]"] == "Hestia Studio"
    assert data["subscription_data[trial_period_days]"] == "14"
    assert data["metadata[plan]"] == "studio"


def test_stripe_portal_uses_customer_ref(monkeypatch, settings):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"url": "https://billing.stripe.test/session"}

    def fake_post(url, *, data, auth, timeout):
        captured.update({"url": url, "data": data, "auth": auth, "timeout": timeout})
        return FakeResponse()

    import httpx

    monkeypatch.setattr(httpx, "post", fake_post)
    s = dataclasses.replace(settings, subscription_backend="stripe", stripe_secret_key="sk_test")
    r = StripeSubscriptions(s).portal(
        tenant={"id": "t1"},
        subscription={"provider_ref": "cus_123"},
        return_url="https://app.test/settings/account",
    )
    assert r.url == "https://billing.stripe.test/session"
    assert captured["url"] == "https://api.stripe.com/v1/billing_portal/sessions"
    assert captured["auth"] == ("sk_test", "")
    assert captured["data"] == {
        "customer": "cus_123",
        "return_url": "https://app.test/settings/account",
    }


def test_stripe_portal_requires_customer_ref(settings):
    s = dataclasses.replace(settings, subscription_backend="stripe", stripe_secret_key="sk_test")
    with pytest.raises(SubscriptionError):
        StripeSubscriptions(s).portal(
            tenant={"id": "t1"},
            subscription={"provider_ref": "sub_123"},
            return_url="https://app.test/settings/account",
        )


def test_apply_plan_updates_tenant_and_records(conn, settings):
    t = create_tenant(conn, name="Plan Co", shoot_type="other")
    apply_plan(conn, t["id"], plan="studio", provider="mock")
    conn.commit()
    assert conn.execute("SELECT plan FROM tenants WHERE id=?", (t["id"],)).fetchone()["plan"] == "studio"
    sub = get_subscription(conn, t["id"])
    assert sub["plan"] == "studio" and sub["status"] == "active" and sub["provider"] == "mock"
    actions = [r["action"] for r in conn.execute(
        "SELECT action FROM audit_log WHERE tenant_id=?", (t["id"],)).fetchall()]
    assert "subscription.changed" in actions


def test_apply_plan_rejects_unknown_plan(conn):
    t = create_tenant(conn, name="Bad Plan", shoot_type="other")
    with pytest.raises(SubscriptionError):
        apply_plan(conn, t["id"], plan="enterprise_unicorn")


# ── webhook event parsing ───────────────────────────────────────────────────


def test_subscription_from_event_parses_completed_checkout():
    ev = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_9",
        "metadata": {"tenant_id": "tA", "plan": "studio"}}}}).encode()
    assert subscription_from_event(ev) == ("tA", "studio", "sub_9")
    ev_customer = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_9", "customer": "cus_9",
        "metadata": {"tenant_id": "tA", "plan": "studio"}}}}).encode()
    assert subscription_from_event(ev_customer) == ("tA", "studio", "cus_9")
    # ignores non-subscription checkouts and unknown plans
    assert subscription_from_event(json.dumps({"type": "checkout.session.completed",
        "data": {"object": {"mode": "payment"}}}).encode()) is None
    assert subscription_from_event(json.dumps({"type": "checkout.session.completed",
        "data": {"object": {"mode": "subscription", "metadata": {"tenant_id": "x", "plan": "nope"}}}}).encode()) is None
    assert subscription_from_event(b"not json") is None


def test_canceled_tenant_from_event():
    ev = json.dumps({"type": "customer.subscription.deleted",
                     "data": {"object": {"metadata": {"tenant_id": "tZ"}}}}).encode()
    assert canceled_tenant_from_event(ev) == "tZ"
    assert canceled_tenant_from_event(json.dumps({"type": "other"}).encode()) is None


# ── HTTP flow ───────────────────────────────────────────────────────────────


def _tid(conn):
    return conn.execute("SELECT id FROM tenants ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_billing_page_lists_plans(client):
    login_owner(client, onboard_studio(client, email="bill@e.com"))
    page = client.get("/settings/billing")
    assert page.status_code == 200
    assert "Hestia Studio" in page.text and "$40/month" in page.text
    assert "Studio Pro" not in page.text


def test_account_page_shows_flat_plan_and_urls(settings):
    app = create_app(dataclasses.replace(
        settings,
        public_url="http://app.hestia.test",
        hosted_domain="hestia.test",
    ))
    client = CSRFClient(app)
    creds = onboard_studio(client, name="Account Studio", email="acct@e.com")
    login_owner(client, creds)
    page = client.get("/settings/account")
    assert page.status_code == 200
    assert "Account" in page.text
    assert "acct@e.com" in page.text
    assert "http://app.hestia.test/studio/account-studio" in page.text
    assert "https://account-studio.hestia.test" in page.text
    assert "$40/mo" in page.text and "Start 14-day trial" in page.text


def test_mock_billing_portal_returns_to_account(client):
    login_owner(client, onboard_studio(client, email="portal@e.com"))
    client.post("/settings/billing/subscribe", data={"plan": "studio"})
    r = client.post("/settings/billing/portal", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "http://testserver/settings/account"


def test_subscribe_switches_plan_in_mock_mode(client, conn):
    login_owner(client, onboard_studio(client, email="sub@e.com"))
    tid = _tid(conn)
    client.post("/settings/billing/subscribe", data={"plan": "studio"})
    assert conn.execute("SELECT plan FROM tenants WHERE id=?", (tid,)).fetchone()["plan"] == "studio"
    assert get_subscription(conn, tid)["status"] == "trialing"


def test_cancel_downgrades_to_beta(client, conn):
    login_owner(client, onboard_studio(client, email="cancel@e.com"))
    tid = _tid(conn)
    client.post("/settings/billing/subscribe", data={"plan": "studio"})
    client.post("/settings/billing/cancel")
    assert conn.execute("SELECT plan FROM tenants WHERE id=?", (tid,)).fetchone()["plan"] == "beta"
    assert get_subscription(conn, tid)["status"] == "canceled"


def test_unknown_plan_is_ignored(client, conn):
    login_owner(client, onboard_studio(client, email="unk@e.com"))
    tid = _tid(conn)
    client.post("/settings/billing/subscribe", data={"plan": "bogus"})
    assert conn.execute("SELECT plan FROM tenants WHERE id=?", (tid,)).fetchone()["plan"] == "beta"


def test_stripe_webhook_activates_subscription(settings, db_path):
    app = create_app(dataclasses.replace(settings, stripe_webhook_secret="whsec_sub"))
    with connect(db_path) as conn:
        t = create_tenant(conn, name="WH Studio", shoot_type="other")
        conn.commit()
    event = json.dumps({"type": "checkout.session.completed", "data": {"object": {
        "mode": "subscription", "subscription": "sub_abc", "customer": "cus_abc",
        "metadata": {"tenant_id": t["id"], "plan": "studio"}}}}).encode()
    header = stripe_signature_header(event, "whsec_sub", timestamp=int(time.time()))

    r = TestClient(app).post("/webhooks/stripe", content=event,
                             headers={"stripe-signature": header})
    assert r.json()["subscription"] == "studio:trialing"
    with connect(db_path) as conn:
        assert conn.execute("SELECT plan FROM tenants WHERE id=?", (t["id"],)).fetchone()["plan"] == "studio"
        assert get_subscription(conn, t["id"])["status"] == "trialing"
        assert get_subscription(conn, t["id"])["provider_ref"] == "cus_abc"

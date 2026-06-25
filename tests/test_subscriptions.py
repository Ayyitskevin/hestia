"""Studio subscriptions — seam selection, plan changes, Stripe webhook activation."""

import dataclasses
import json
import time

import pytest
from conftest import login_owner, onboard_studio
from fastapi.testclient import TestClient

from hestia.db import connect
from hestia.main import create_app
from hestia.payments import stripe_signature_header
from hestia.subscriptions import (
    MockSubscriptions,
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


def test_stripe_subscribe_needs_key_and_price(settings):
    s = dataclasses.replace(settings, subscription_backend="stripe", stripe_secret_key="",
                            stripe_price_studio="")
    with pytest.raises(SubscriptionError):
        StripeSubscriptions(s).subscribe(tenant={"id": "t1"}, plan="studio", success_url="/x")


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
        "metadata": {"tenant_id": "tA", "plan": "studio_pro"}}}}).encode()
    assert subscription_from_event(ev) == ("tA", "studio_pro", "sub_9")
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
    assert "Studio Pro" in page.text and "Current plan" in page.text


def test_subscribe_switches_plan_in_mock_mode(client, conn):
    login_owner(client, onboard_studio(client, email="sub@e.com"))
    tid = _tid(conn)
    client.post("/settings/billing/subscribe", data={"plan": "studio"})
    assert conn.execute("SELECT plan FROM tenants WHERE id=?", (tid,)).fetchone()["plan"] == "studio"
    assert get_subscription(conn, tid)["status"] == "active"


def test_cancel_downgrades_to_beta(client, conn):
    login_owner(client, onboard_studio(client, email="cancel@e.com"))
    tid = _tid(conn)
    client.post("/settings/billing/subscribe", data={"plan": "studio_pro"})
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
        "mode": "subscription", "subscription": "sub_abc",
        "metadata": {"tenant_id": t["id"], "plan": "studio"}}}}).encode()
    header = stripe_signature_header(event, "whsec_sub", timestamp=int(time.time()))

    r = TestClient(app).post("/webhooks/stripe", content=event,
                             headers={"stripe-signature": header})
    assert r.json()["subscription"] == "studio:active"
    with connect(db_path) as conn:
        assert conn.execute("SELECT plan FROM tenants WHERE id=?", (t["id"],)).fetchone()["plan"] == "studio"

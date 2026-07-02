"""Stripe webhook — signature verification + the checkout→paid money path."""

import dataclasses
import json
import time

from conftest import CSRFClient, login_owner, onboard_studio
from fastapi.testclient import TestClient

from hestia.main import create_app
from hestia.payments import (
    checkout_token_from_event,
    stripe_signature_header,
    verify_stripe_signature,
)

SECRET = "whsec_test_123"


# ── Signature verification ──────────────────────────────────────────────────


def test_signature_roundtrip_valid():
    payload = b'{"hello":"world"}'
    header = stripe_signature_header(payload, SECRET, timestamp=int(time.time()))
    assert verify_stripe_signature(payload, header, SECRET) is True


def test_signature_rejects_tampered_payload():
    payload = b'{"amount":100}'
    header = stripe_signature_header(payload, SECRET, timestamp=int(time.time()))
    assert verify_stripe_signature(b'{"amount":999}', header, SECRET) is False


def test_signature_rejects_wrong_secret():
    payload = b"{}"
    header = stripe_signature_header(payload, SECRET, timestamp=int(time.time()))
    assert verify_stripe_signature(payload, header, "whsec_other") is False


def test_signature_rejects_missing_or_malformed():
    assert verify_stripe_signature(b"{}", "", SECRET) is False
    assert verify_stripe_signature(b"{}", "garbage", SECRET) is False


def test_signature_replay_window():
    payload = b"{}"
    old = stripe_signature_header(payload, SECRET, timestamp=int(time.time()) - 10_000)
    assert verify_stripe_signature(payload, old, SECRET, tolerance=300) is False
    assert verify_stripe_signature(payload, old, SECRET, tolerance=0) is True  # window disabled


def test_token_extraction():
    ev = json.dumps({"type": "checkout.session.completed",
                     "data": {"object": {"client_reference_id": "tok_abc"}}}).encode()
    assert checkout_token_from_event(ev) == "tok_abc"
    ev2 = json.dumps({"type": "checkout.session.completed",
                      "data": {"object": {"metadata": {"invoice_token": "tok_xyz"}}}}).encode()
    assert checkout_token_from_event(ev2) == "tok_xyz"
    assert checkout_token_from_event(json.dumps({"type": "other"}).encode()) is None
    assert checkout_token_from_event(b"not json") is None


# ── Full webhook → invoice paid ─────────────────────────────────────────────


def _app_with_webhook(settings):
    return create_app(dataclasses.replace(settings, stripe_webhook_secret=SECRET))


def _invoice_token(client, iid):
    detail = client.get(f"/invoices/{iid}")
    return detail.text.split("/pay/")[1].split('"')[0].split("<")[0].strip()


def test_webhook_marks_invoice_paid(settings):
    app = _app_with_webhook(settings)
    studio = TestClient(app)
    creds = onboard_studio(studio, email="wh@example.com")
    owner = login_owner(CSRFClient(app), creds)
    iid = owner.post("/invoices", data={"title": "Balance", "amount": "1000"}).url.path.split("/")[-1]
    token = _invoice_token(owner, iid)

    event = json.dumps({"type": "checkout.session.completed",
                        "data": {"object": {"id": "cs_test", "client_reference_id": token}}}).encode()
    header = stripe_signature_header(event, SECRET, timestamp=int(time.time()))

    hook = TestClient(app)
    r = hook.post("/webhooks/stripe", content=event, headers={"stripe-signature": header})
    assert r.status_code == 200 and r.json()["paid"] is True
    assert "Paid" in owner.get(f"/pay/{token}").text

    # idempotent: a duplicate delivery does not re-settle
    r2 = hook.post("/webhooks/stripe", content=event, headers={"stripe-signature": header})
    assert r2.json()["paid"] is False


def test_webhook_rejects_bad_signature(settings):
    app = _app_with_webhook(settings)
    event = json.dumps({"type": "checkout.session.completed", "data": {"object": {}}}).encode()
    r = TestClient(app).post("/webhooks/stripe", content=event,
                             headers={"stripe-signature": "t=1,v1=deadbeef"})
    assert r.status_code == 400


def test_webhook_503_when_unconfigured(client):
    # default settings have no webhook secret
    r = client.post("/webhooks/stripe", content=b"{}", headers={"stripe-signature": "x"})
    assert r.status_code == 503


# ── Subscription lifecycle sync ─────────────────────────────────────────────


def _signed(event: dict) -> tuple[bytes, dict]:
    payload = json.dumps(event).encode()
    header = stripe_signature_header(payload, SECRET, timestamp=int(time.time()))
    return payload, {"stripe-signature": header}


def _sub_event(event_type: str, *, tenant_id: str, status: str = "") -> dict:
    obj = {"metadata": {"tenant_id": tenant_id, "plan": "studio"}}
    if status:
        obj["status"] = status
    return {"type": event_type, "data": {"object": obj}}


def test_webhook_syncs_trial_conversion_and_dunning(settings, conn):
    """The full lifecycle Stripe owns after checkout: trialing → active on real
    payment, active → past_due on a failed card — plan untouched throughout."""
    from hestia.subscriptions import apply_plan, get_subscription
    from hestia.tenants import create_tenant, get_tenant

    app = _app_with_webhook(settings)
    t = create_tenant(conn, name="Lifecycle Studio", shoot_type="wedding")
    apply_plan(conn, t["id"], plan="studio", status="trialing", provider="stripe")
    conn.commit()
    hook = TestClient(app)

    payload, headers = _signed(_sub_event("customer.subscription.updated",
                                          tenant_id=t["id"], status="active"))
    r = hook.post("/webhooks/stripe", content=payload, headers=headers)
    assert r.status_code == 200 and r.json()["subscription"] == "studio:active"
    assert get_subscription(conn, t["id"])["status"] == "active"
    assert get_tenant(conn, t["id"])["plan"] == "studio"

    # replayed delivery: still 200, still synced, no error
    r2 = hook.post("/webhooks/stripe", content=payload, headers=headers)
    assert r2.status_code == 200 and r2.json()["subscription"] == "studio:active"

    payload, headers = _signed(_sub_event("customer.subscription.updated",
                                          tenant_id=t["id"], status="past_due"))
    r3 = hook.post("/webhooks/stripe", content=payload, headers=headers)
    assert r3.status_code == 200 and r3.json()["subscription"] == "studio:past_due"
    assert get_subscription(conn, t["id"])["status"] == "past_due"
    assert get_tenant(conn, t["id"])["plan"] == "studio"     # grace period, no downgrade


def test_webhook_ignores_unknown_tenants_instead_of_500ing(settings):
    """Bogus or foreign metadata must be acknowledged (200), not retried for days:
    with foreign keys ON, an unguarded apply_plan would raise and 500."""
    app = _app_with_webhook(settings)
    hook = TestClient(app)

    for event in (
        _sub_event("customer.subscription.updated", tenant_id="ghost", status="active"),
        _sub_event("customer.subscription.deleted", tenant_id="ghost"),
        {"type": "checkout.session.completed",
         "data": {"object": {"mode": "subscription",
                             "metadata": {"tenant_id": "ghost", "plan": "studio"}}}},
    ):
        payload, headers = _signed(event)
        r = hook.post("/webhooks/stripe", content=payload, headers=headers)
        assert r.status_code == 200
        assert r.json()["subscription"] is None


def test_webhook_cancel_downgrades_known_tenant(settings, conn):
    from hestia.subscriptions import apply_plan, get_subscription
    from hestia.tenants import create_tenant, get_tenant

    app = _app_with_webhook(settings)
    t = create_tenant(conn, name="Cancel Studio", shoot_type="wedding")
    apply_plan(conn, t["id"], plan="studio", status="active", provider="stripe")
    conn.commit()

    payload, headers = _signed(_sub_event("customer.subscription.deleted", tenant_id=t["id"]))
    r = TestClient(app).post("/webhooks/stripe", content=payload, headers=headers)
    assert r.status_code == 200 and r.json()["subscription"] == "beta:canceled"
    assert get_tenant(conn, t["id"])["plan"] == "beta"
    assert get_subscription(conn, t["id"])["status"] == "canceled"

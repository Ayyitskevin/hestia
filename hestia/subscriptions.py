"""Studio subscription seam — billing the studios themselves (essence of a SaaS).

Pluggable, same shape as the payments seam:

- ``mock``   — activates the chosen plan instantly, no charge. The default, so the
  whole change-plan flow is testable and demoable.
- ``stripe`` — opens a Stripe Checkout Session in *subscription* mode at Hestia's
  flat $40/month price; the plan is activated when Stripe confirms via webhook.

The authoritative plan lives on ``tenants.plan``; :func:`apply_plan` updates it and
upserts the ``subscriptions`` row, recording an audit entry. Nothing charges money
unless ``subscription_backend=stripe`` with real keys.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .billing import PLANS
from .config import Settings
from .db import audit


class SubscriptionError(RuntimeError):
    pass


@dataclass
class SubscribeResult:
    url: str             # where to send the owner next
    activated: bool      # mock activates now; stripe activates via webhook
    simulated: bool = False


@dataclass
class PortalResult:
    url: str
    simulated: bool = False


# ── data access ─────────────────────────────────────────────────────────────


def get_subscription(conn: sqlite3.Connection, tenant_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM subscriptions WHERE tenant_id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def apply_plan(conn: sqlite3.Connection, tenant_id: str, *, plan: str, status: str = "active",
               provider: str = "mock", provider_ref: str = "") -> None:
    """Move a tenant to ``plan``: update tenants.plan + upsert the subscription row."""
    if plan not in PLANS:
        raise SubscriptionError(f"unknown plan '{plan}'")
    conn.execute("UPDATE tenants SET plan = ? WHERE id = ?", (plan, tenant_id))
    conn.execute(
        """
        INSERT INTO subscriptions (tenant_id, plan, status, provider, provider_ref, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (tenant_id) DO UPDATE SET
            plan = excluded.plan, status = excluded.status, provider = excluded.provider,
            provider_ref = excluded.provider_ref, updated_at = datetime('now')
        """,
        (tenant_id, plan, status, provider, provider_ref),
    )
    audit(conn, actor="owner", action="subscription.changed", tenant_id=tenant_id,
          detail=f"{plan} ({status}) via {provider}")


# ── seam ────────────────────────────────────────────────────────────────────


class MockSubscriptions:
    backend = "mock"

    def subscribe(self, *, tenant: dict, plan: str, success_url: str) -> SubscribeResult:
        # No external call; the plan "activates" immediately and we return to billing.
        return SubscribeResult(url=success_url, activated=True, simulated=True)

    def portal(self, *, tenant: dict, subscription: dict | None, return_url: str) -> PortalResult:
        return PortalResult(url=return_url, simulated=True)


class StripeSubscriptions:
    backend = "stripe"

    def __init__(self, settings: Settings):
        self.settings = settings

    def subscribe(self, *, tenant: dict, plan: str, success_url: str) -> SubscribeResult:
        import httpx

        if plan != "studio":
            raise SubscriptionError("stripe subscriptions are only available for the flat studio plan")
        if not self.settings.stripe_secret_key:
            raise SubscriptionError("stripe subscription backend needs a secret key")
        data = {
            "mode": "subscription",
            "success_url": success_url,
            "cancel_url": success_url,
            "client_reference_id": tenant["id"],
            "customer_email": tenant.get("owner_email", ""),
            "line_items[0][price_data][currency]": self.settings.currency,
            "line_items[0][price_data][unit_amount]": str(self.settings.flat_price_cents),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][price_data][product_data][name]": "Hestia Studio",
            "line_items[0][quantity]": "1",
            "metadata[tenant_id]": tenant["id"],
            "metadata[plan]": "studio",
            "subscription_data[metadata][tenant_id]": tenant["id"],
            "subscription_data[metadata][plan]": "studio",
            "subscription_data[trial_period_days]": str(max(0, int(self.settings.trial_days))),
        }
        if not data["customer_email"]:
            data.pop("customer_email")
        try:
            resp = httpx.post("https://api.stripe.com/v1/checkout/sessions", data=data,
                              auth=(self.settings.stripe_secret_key, ""), timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SubscriptionError(f"stripe checkout failed: {exc}") from exc
        return SubscribeResult(url=body["url"], activated=False)

    def portal(self, *, tenant: dict, subscription: dict | None, return_url: str) -> PortalResult:
        import httpx

        if not self.settings.stripe_secret_key:
            raise SubscriptionError("stripe subscription backend needs a secret key")
        customer = ((subscription or {}).get("provider_ref") or "").strip()
        if not customer.startswith("cus_"):
            raise SubscriptionError("stripe billing portal needs a customer reference")
        data = {"customer": customer, "return_url": return_url}
        try:
            resp = httpx.post("https://api.stripe.com/v1/billing_portal/sessions", data=data,
                              auth=(self.settings.stripe_secret_key, ""), timeout=30)
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise SubscriptionError(f"stripe billing portal failed: {exc}") from exc
        return PortalResult(url=body["url"])


def build_subscriptions(settings: Settings):
    if settings.subscription_backend == "stripe":
        return StripeSubscriptions(settings)
    return MockSubscriptions()


# ── Stripe webhook event parsing (subscription lifecycle) ───────────────────


def subscription_from_event(payload: bytes) -> tuple[str, str, str] | None:
    """(tenant_id, plan, ref) from a completed subscription checkout, else None."""
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    if event.get("type") != "checkout.session.completed":
        return None
    obj = (event.get("data") or {}).get("object") or {}
    if obj.get("mode") != "subscription":
        return None
    meta = obj.get("metadata") or {}
    tenant_id, plan = meta.get("tenant_id"), meta.get("plan")
    if not tenant_id or plan not in PLANS:
        return None
    return tenant_id, plan, obj.get("customer") or obj.get("subscription") or obj.get("id") or ""


def canceled_tenant_from_event(payload: bytes) -> str | None:
    """tenant_id from a ``customer.subscription.deleted`` event, else None."""
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    if event.get("type") != "customer.subscription.deleted":
        return None
    obj = (event.get("data") or {}).get("object") or {}
    return (obj.get("metadata") or {}).get("tenant_id")


# Stripe subscription status → Hestia subscription status. Statuses Stripe owns
# after checkout: the trial→active conversion and payment failure arrive ONLY on
# customer.subscription.updated. `canceled` is deliberately absent — the terminal
# customer.subscription.deleted event owns the downgrade.
_STATUS_SYNC = {
    "trialing": "trialing",
    "active": "active",
    "past_due": "past_due",
    "unpaid": "past_due",
}


def subscription_status_from_event(payload: bytes) -> tuple[str, str] | None:
    """(tenant_id, status) from a ``customer.subscription.updated`` event, else None.

    Without this sync a studio that converts trial→paid stays 'trialing' in
    Hestia forever (and keeps drawing trial-ending nudges), and a failed card
    (past_due) keeps silent full access with no operator signal."""
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    if event.get("type") != "customer.subscription.updated":
        return None
    obj = (event.get("data") or {}).get("object") or {}
    tenant_id = (obj.get("metadata") or {}).get("tenant_id")
    status = _STATUS_SYNC.get(obj.get("status") or "")
    if not tenant_id or not status:
        return None
    return tenant_id, status


def set_subscription_status(conn: sqlite3.Connection, tenant_id: str, *, status: str,
                            provider: str = "stripe") -> bool:
    """Sync ONLY the subscription row's status — the plan is untouched (past_due
    keeps access as a grace period; downgrades stay with the deleted event).
    Returns False for unknown tenants or missing subscription rows, so bogus or
    foreign webhook metadata can't 500 the endpoint or write orphan rows."""
    row = conn.execute(
        "SELECT s.status FROM subscriptions s JOIN tenants t ON t.id = s.tenant_id "
        "WHERE s.tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    if not row:
        return False
    if row["status"] == status:
        return True                     # replayed delivery — nothing to write
    conn.execute(
        "UPDATE subscriptions SET status = ?, provider = ?, updated_at = datetime('now') "
        "WHERE tenant_id = ?",
        (status, provider, tenant_id),
    )
    audit(conn, actor="stripe", action="subscription.status_synced", tenant_id=tenant_id,
          detail=status)
    return True

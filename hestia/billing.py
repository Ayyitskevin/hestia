"""Plan catalog + per-tenant plan status.

The plans a studio can be on (Beta / Studio / Studio Pro) and how to read a
tenant's current plan. The subscription *engine* — activating, canceling, and the
mock|stripe seam + webhook — lives in :mod:`hestia.subscriptions`; this module is
just the catalog those flows price against.
"""

from __future__ import annotations

from dataclasses import dataclass

PLANS = {
    "beta": {"name": "Beta", "price": "$0", "galleries": None, "blurb": "Invite-only beta — everything on."},
    "studio": {"name": "Studio", "price": "$29/mo", "galleries": 100, "blurb": "For working photographers."},
    "studio_pro": {"name": "Studio Pro", "price": "$79/mo", "galleries": None, "blurb": "Unlimited galleries + priority vision."},
}


@dataclass
class PlanStatus:
    plan: str
    name: str
    price: str
    blurb: str
    live: bool = False  # Stripe wired? Phase 1.


def plan_status(tenant: dict) -> PlanStatus:
    plan = tenant.get("plan", "beta")
    meta = PLANS.get(plan, PLANS["beta"])
    return PlanStatus(plan=plan, name=meta["name"], price=meta["price"], blurb=meta["blurb"], live=False)

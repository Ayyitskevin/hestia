"""Flat-price studio plan catalog.

Hestia is intentionally a one-price MicroSaaS: a 14-day trial, then Hestia Studio
at $40/month. The subscription engine lives in :mod:`hestia.subscriptions`; this
module is the catalog every billing surface reads from.
"""

from __future__ import annotations

from dataclasses import dataclass

PLANS = {
    "beta": {
        "name": "Trial",
        "price": "$0",
        "galleries": None,
        "blurb": "14-day hosted trial — all features included.",
    },
    "studio": {
        "name": "Hestia Studio",
        "price": "$40/mo",
        "galleries": None,
        "blurb": "Everything needed to run a professional photography studio.",
    },
}


@dataclass
class PlanStatus:
    plan: str
    name: str
    price: str
    blurb: str
    live: bool = False  # True when a real subscription backend (Stripe) is wired


def plan_status(tenant: dict, *, subscription_backend: str | None = None) -> PlanStatus:
    plan = tenant.get("plan", "beta")
    if plan == "studio_pro":  # legacy tenants from the old catalog keep full access
        plan = "studio"
    meta = PLANS.get(plan, PLANS["beta"])
    live = (subscription_backend or "").strip().lower() == "stripe"
    return PlanStatus(plan=plan, name=meta["name"], price=meta["price"], blurb=meta["blurb"], live=live)

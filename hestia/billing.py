"""Billing scaffold (Phase 1 wires live Stripe).

Honest placeholder: one plan per tenant today, and the seam where a Stripe
Customer-per-tenant subscription and offer checkout will attach. Nothing here
charges money yet — it exists so the data model and UI have a real place to grow
into, not to pretend billing is done.
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

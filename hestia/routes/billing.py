"""Studio billing — view plans and subscribe / cancel via the subscription seam."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..billing import PLANS, plan_status
from ..subscriptions import (
    SubscriptionError,
    apply_plan,
    build_subscriptions,
    get_subscription,
)
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.get("/settings/billing")
def billing(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        sub = get_subscription(conn, tenant["id"])
    return render(request, "billing.html", auth=auth, tenant=tenant, plan=plan_status(tenant),
                  plans=PLANS, subscription=sub, backend=settings.subscription_backend)


@router.post("/settings/billing/subscribe")
def subscribe(request: Request, plan: str = Form(...)):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if plan not in PLANS:
            return RedirectResponse("/settings/billing", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        provider = build_subscriptions(settings)
        success_url = f"{settings.public_url.rstrip('/')}/settings/billing"
        try:
            result = provider.subscribe(tenant=tenant, plan=plan, success_url=success_url)
        except SubscriptionError:
            return RedirectResponse("/settings/billing", status_code=303)
        if result.activated:  # mock: live immediately. stripe: activated by the webhook.
            apply_plan(conn, tenant["id"], plan=plan, provider=provider.backend)
    return RedirectResponse(result.url, status_code=303)


@router.post("/settings/billing/cancel")
def cancel(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        # Downgrade to the free Beta plan. (Stripe cancellation also arrives via webhook.)
        apply_plan(conn, auth.tenant["id"], plan="beta", status="canceled",
                   provider=build_subscriptions(settings).backend)
    return RedirectResponse("/settings/billing", status_code=303)

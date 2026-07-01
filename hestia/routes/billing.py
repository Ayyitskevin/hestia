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


def _owner_email(conn, tenant_id: str) -> str:
    row = conn.execute(
        "SELECT email FROM users WHERE tenant_id = ? AND role = 'owner' ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return row["email"] if row else ""


def _hosted_url(settings, tenant: dict) -> str:
    domain = (settings.hosted_domain or "").strip().strip(".")
    if not domain:
        return ""
    return f"https://{tenant['slug']}.{domain}"


@router.get("/settings/account")
def account(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        sub = get_subscription(conn, tenant["id"])
        owner_email = _owner_email(conn, tenant["id"])
    studio_url = f"{settings.public_url.rstrip('/')}/studio/{tenant['slug']}"
    return render(
        request,
        "account.html",
        auth=auth,
        tenant=tenant,
        plan=plan_status(tenant),
        subscription=sub,
        backend=settings.subscription_backend,
        owner_email=owner_email,
        studio_url=studio_url,
        hosted_url=_hosted_url(settings, tenant),
    )


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
        if plan != "studio" or plan not in PLANS:
            return RedirectResponse("/settings/billing", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        tenant = {**tenant, "owner_email": _owner_email(conn, tenant["id"])}
        provider = build_subscriptions(settings)
        success_url = f"{settings.public_url.rstrip('/')}/settings/billing"
        try:
            result = provider.subscribe(tenant=tenant, plan=plan, success_url=success_url)
        except SubscriptionError:
            return RedirectResponse("/settings/billing", status_code=303)
        if result.activated:  # mock: live immediately. stripe: activated by the webhook.
            status = "trialing" if settings.trial_days > 0 else "active"
            apply_plan(conn, tenant["id"], plan=plan, status=status, provider=provider.backend)
    return RedirectResponse(result.url, status_code=303)


@router.post("/settings/billing/portal")
def billing_portal(request: Request):
    settings = settings_of(request)
    return_url = f"{settings.public_url.rstrip('/')}/settings/account"
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        sub = get_subscription(conn, tenant["id"])
        provider = build_subscriptions(settings)
        try:
            result = provider.portal(tenant=tenant, subscription=sub, return_url=return_url)
        except SubscriptionError:
            return RedirectResponse("/settings/account", status_code=303)
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

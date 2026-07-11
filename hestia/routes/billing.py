"""Studio billing — view plans and subscribe / cancel via the subscription seam."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import ADMIN
from ..billing import PLANS, plan_status
from ..domains import custom_domain_summary, set_custom_domain, verify_custom_domain_dns
from ..subscriptions import (
    SubscriptionError,
    apply_plan,
    build_subscriptions,
    get_subscription,
)
from ..tenants import (
    create_user,
    delete_tenant_user,
    get_tenant,
    get_user_by_email,
    list_tenant_users,
)
from .deps import db_conn, owner_only, render, settings_of, tenant_user

router = APIRouter()




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
def account(request: Request, dns: str = ""):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        tenant = get_tenant(conn, auth.tenant["id"])
        sub = get_subscription(conn, tenant["id"])
        owner_email = _owner_email(conn, tenant["id"])
        custom_domain = custom_domain_summary(settings, tenant)
    studio_url = f"{settings.public_url.rstrip('/')}/studio/{tenant['slug']}"
    dns_messages = {
        "verified": "DNS verified — your custom domain is live.",
        "no-match": "TXT record not found or it doesn't match the token yet. "
                    "DNS can take a few minutes to propagate; try again shortly.",
        "unavailable": "Couldn't run a DNS check from this host. "
                       "Ask the operator to mark the domain verified.",
        "unset": "Save a custom domain before checking DNS.",
    }
    return render(
        request,
        "account.html",
        auth=auth,
        tenant=tenant,
        plan=plan_status(tenant, subscription_backend=settings.subscription_backend),
        subscription=sub,
        backend=settings.subscription_backend,
        owner_email=owner_email,
        studio_url=studio_url,
        hosted_url=_hosted_url(settings, tenant),
        custom_domain=custom_domain,
        domain_error="",
        dns_message=dns_messages.get(dns, ""),
    )


@router.post("/settings/account/domain")
def account_domain(request: Request, custom_domain: str = Form("")):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        try:
            set_custom_domain(
                conn,
                auth.tenant["id"],
                custom_domain,
                hosted_domain=settings.hosted_domain,
            )
        except ValueError:
            tenant = get_tenant(conn, auth.tenant["id"])
            sub = get_subscription(conn, tenant["id"])
            owner_email = _owner_email(conn, tenant["id"])
            studio_url = f"{settings.public_url.rstrip('/')}/studio/{tenant['slug']}"
            return render(
                request,
                "account.html",
                auth=auth,
                tenant=tenant,
                plan=plan_status(tenant, subscription_backend=settings.subscription_backend),
                subscription=sub,
                backend=settings.subscription_backend,
                owner_email=owner_email,
                studio_url=studio_url,
                hosted_url=_hosted_url(settings, tenant),
                custom_domain=custom_domain_summary(settings, tenant),
                domain_error="Enter a valid domain you control, like photos.example.com.",
                dns_message="",
                status_code=400,
            )
    return RedirectResponse("/settings/account", status_code=303)


@router.post("/settings/account/domain/check")
def account_domain_check_dns(request: Request):
    """Owner self-serve DNS check: look up the verification TXT record and flip
    to verified on an exact token match, so pro studios don't wait on the founder
    to click Mark verified. Banners the outcome via a query param."""
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        result = verify_custom_domain_dns(conn, auth.tenant["id"])
        conn.commit()
    return RedirectResponse(f"/settings/account?dns={result['status']}", status_code=303)


@router.get("/settings/billing")
def billing(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        tenant = get_tenant(conn, auth.tenant["id"])
        sub = get_subscription(conn, tenant["id"])
    return render(request, "billing.html", auth=auth, tenant=tenant, plan=plan_status(tenant, subscription_backend=settings.subscription_backend),
                  plans=PLANS, subscription=sub, backend=settings.subscription_backend)


@router.post("/settings/billing/subscribe")
def subscribe(request: Request, plan: str = Form(...)):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
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
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
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
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        # Nothing to cancel on the free plan — a no-op keeps repeat POSTs from writing
        # junk 'canceled' subscription rows and audit noise.
        if get_tenant(conn, auth.tenant["id"])["plan"] == "beta":
            return RedirectResponse("/settings/billing", status_code=303)
        # Downgrade to the free Beta plan. (Stripe cancellation also arrives via webhook.)
        apply_plan(conn, auth.tenant["id"], plan="beta", status="canceled",
                   provider=build_subscriptions(settings).backend)
    return RedirectResponse("/settings/billing", status_code=303)


# ── Team — owner-only multi-admin management ─────────────────────────────────


@router.get("/settings/team")
def team(request: Request, invited: str = "", error: str = ""):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        tenant = get_tenant(conn, auth.tenant["id"])
        users = list_tenant_users(conn, auth.tenant["id"])
    return render(request, "team.html", auth=auth, tenant=tenant, users=users,
                  invited=invited, error=error)


@router.post("/settings/team/invite")
def team_invite(request: Request, email: str = Form(...), password: str = Form(...)):
    """Add a secondary studio admin. The owner provides the invitee's email and a
    temporary password (communicated out-of-band); a proper emailed setup link is
    a future step. The owner stays the sole account holder."""
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        email_clean = (email or "").strip().lower()
        if not email_clean or not (password or "").strip() or len(password) < 8:
            return _team_render_error(request, auth, "Add a valid email and a password of at least 8 characters.")
        if get_user_by_email(conn, email_clean):
            return _team_render_error(request, auth, "That email is already on this or another account.")
        create_user(conn, tenant_id=auth.tenant["id"], email=email_clean,
                    password=password, role=ADMIN)
        conn.commit()
    return RedirectResponse("/settings/team?invited=1", status_code=303)


@router.post("/settings/team/{user_id}/remove")
def team_remove(request: Request, user_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if forbid := owner_only(auth):
            return forbid
        delete_tenant_user(conn, auth.tenant["id"], user_id)
        conn.commit()
    return RedirectResponse("/settings/team", status_code=303)


def _team_render_error(request: Request, auth, message: str):
    with db_conn(request) as conn:
        tenant = get_tenant(conn, auth.tenant["id"])
        users = list_tenant_users(conn, auth.tenant["id"])
    return render(request, "team.html", auth=auth, tenant=tenant, users=users,
                  invited="", error=message, status_code=400)

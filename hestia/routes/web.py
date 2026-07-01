"""Public + session web UI: landing, login/logout, dashboard."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    authenticate_user,
    context_from_session,
    cookie_is_secure,
    create_session,
    destroy_session,
)
from ..billing import plan_status
from ..booking import list_booking_types
from ..crm import list_clients, list_projects
from ..dashboard import (
    money_snapshot,
    needs_attention,
    reconnect_due,
    send_owner_digest_now,
    setup_checklist,
    trial_cockpit,
)
from ..db import audit
from ..demo import demo_nav, demo_tour
from ..email import notify
from ..galleries import list_galleries
from ..hosted import tenant_from_custom_domain, tenant_slug_from_request
from ..interest import record_beta_interest
from ..invoices import money
from ..packages import list_packages
from ..pipeline import list_runs
from ..presets import preset_applied
from ..proposals import proposal_metrics
from ..ratelimit import enforce
from ..resets import consume_reset, create_reset, find_reset
from ..studio import get_profile
from ..subscriptions import get_subscription
from ..tenants import (
    create_tenant,
    create_user,
    get_tenant,
    get_tenant_by_slug,
    get_user,
    get_user_by_email,
    mark_user_verified,
    set_user_password,
    signup_attribution,
    tenant_flags,
)
from ..testimonials import featured_testimonials
from ..verifications import consume_verification, create_verification
from .deps import db_conn, render, settings_of

router = APIRouter()


def _owner_home(conn, tenant_id: str) -> str:
    """First-run studios go straight to presets; configured studios go home."""
    return "/dashboard" if preset_applied(conn, tenant_id) else "/onboarding"


def _session_redirect(settings, token: str, target: str) -> RedirectResponse:
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=cookie_is_secure(settings), max_age=int(SESSION_TTL.total_seconds()))
    return resp


@router.get("/")
def landing(request: Request):
    with db_conn(request) as conn:
        tenant = None
        if slug := tenant_slug_from_request(request):
            tenant = get_tenant_by_slug(conn, slug)
            if not tenant:
                return render(request, "offer_missing.html", auth=None, status_code=404)
        else:
            tenant = tenant_from_custom_domain(conn, request)
        if tenant:
            profile = get_profile(conn, tenant["id"])
            if not profile["published"]:
                return render(request, "studio/coming_soon.html", auth=None, tenant=tenant)
            testimonials = featured_testimonials(conn, tenant["id"])
            currency = settings_of(request).currency
            packages = list_packages(conn, tenant["id"], active_only=True)
            for p in packages:
                p["price_display"] = money(p["price_cents"], currency)
            has_booking = bool(list_booking_types(conn, tenant["id"], active_only=True))
            return render(request, "studio/site.html", auth=None, tenant=tenant,
                          profile=profile, testimonials=testimonials, ref="",
                          packages=packages, has_booking=has_booking)
        auth = context_from_session(conn, request)
    return render(request, "landing.html", auth=auth)


@router.get("/demo")
def demo(request: Request, niche: str = "wedding"):
    return render(request, "demo.html", auth=None, tour=demo_tour(niche), demos=demo_nav())


@router.get("/demo/{niche}")
def demo_niche(request: Request, niche: str):
    return render(request, "demo.html", auth=None, tour=demo_tour(niche), demos=demo_nav())


@router.get("/pricing")
def pricing(request: Request):
    settings = settings_of(request)
    price = f"${settings.flat_price_cents // 100}/month"
    return render(request, "pricing.html", auth=None, price=price, trial_days=settings.trial_days)


@router.get("/interest")
def interest_form(request: Request, source: str = "", path: str = ""):
    attribution = signup_attribution(source, path)
    return render(
        request,
        "interest.html",
        auth=None,
        sent=False,
        error=None,
        interest=None,
        signup_source=attribution["source"],
        signup_landing_path=attribution["landing_path"],
    )


@router.post("/interest")
def interest_submit(
    request: Request,
    name: str = Form(""),
    studio_name: str = Form(""),
    email: str = Form(...),
    shoot_type: str = Form("other"),
    note: str = Form(""),
    signup_source: str = Form(""),
    signup_landing_path: str = Form(""),
):
    enforce(request, "interest")
    settings = settings_of(request)
    attribution = signup_attribution(signup_source, signup_landing_path)

    def _again(error: str):
        return render(
            request,
            "interest.html",
            auth=None,
            sent=False,
            error=error,
            interest=None,
            signup_source=attribution["source"],
            signup_landing_path=attribution["landing_path"],
        )

    with db_conn(request) as conn:
        try:
            interest = record_beta_interest(
                conn,
                settings,
                name=name,
                studio_name=studio_name,
                email=email,
                shoot_type=shoot_type,
                note=note,
                source=attribution["source"],
                landing_path=attribution["landing_path"],
            )
        except ValueError as exc:
            return _again(str(exc))
    return render(
        request,
        "interest.html",
        auth=None,
        sent=True,
        error=None,
        interest=interest,
        signup_source=attribution["source"],
        signup_landing_path=attribution["landing_path"],
    )


@router.get("/login")
def login_form(request: Request):
    return render(request, "login.html", auth=None, error=None)


@router.post("/login")
def login_submit(request: Request, email: str = Form(...), password: str = Form(...)):
    enforce(request, "login")
    settings = settings_of(request)
    with db_conn(request) as conn:
        user = authenticate_user(conn, email, password)
        if not user or not user["tenant_id"]:
            return render(request, "login.html", auth=None, error="Invalid email or password.")
        if not user["verified"]:
            return render(request, "login.html", auth=None,
                          error="Please verify your email — we sent you a link when you signed up.")
        token = create_session(conn, role=user["role"], user_id=user["id"],
                               tenant_id=user["tenant_id"])
        target = _owner_home(conn, user["tenant_id"])
    return _session_redirect(settings, token, target)


@router.get("/signup")
def signup_form(request: Request, source: str = "", path: str = ""):
    if not settings_of(request).signup_enabled:
        return RedirectResponse("/login", status_code=303)
    attribution = signup_attribution(source, path)
    return render(request, "signup.html", auth=None, sent=False, error=None,
                  signup_source=attribution["source"],
                  signup_landing_path=attribution["landing_path"])


@router.post("/signup")
def signup_submit(request: Request, name: str = Form(...), email: str = Form(...),
                  password: str = Form(...), shoot_type: str = Form("other"),
                  signup_source: str = Form(""), signup_landing_path: str = Form("")):
    settings = settings_of(request)
    if not settings.signup_enabled:
        return RedirectResponse("/login", status_code=303)
    enforce(request, "signup")

    attribution = signup_attribution(signup_source, signup_landing_path)

    def _again(error: str):
        return render(request, "signup.html", auth=None, sent=False, error=error,
                      signup_source=attribution["source"],
                      signup_landing_path=attribution["landing_path"])

    with db_conn(request) as conn:
        email_norm = email.strip().lower()
        if len(password) < 8:
            return _again("Choose a password of at least 8 characters.")
        if get_user_by_email(conn, email_norm):
            return _again("That email is already registered — try signing in instead.")
        tenant = create_tenant(conn, name=name, shoot_type=shoot_type,
                               signup_source=attribution["source"],
                               signup_landing_path=attribution["landing_path"])
        user = create_user(conn, tenant_id=tenant["id"], email=email_norm,
                           password=password, role="owner", verified=0)
        token = create_verification(conn, settings, user_id=user["id"])
        link = f"{settings.public_url.rstrip('/')}/verify/{token}"
        notify(conn, settings, to=email_norm, tenant_id=tenant["id"], signed=False,
               subject="Verify your email to activate your Hestia studio",
               body=(f"Welcome to Hestia!\n\nConfirm your email to activate "
                     f"{tenant['name']}:\n{link}\n\nThis link expires in 2 days. "
                     f"If you didn't sign up, you can ignore this email."))
    return render(request, "signup.html", auth=None, sent=True, error=None)


@router.get("/verify/{token}")
def verify_email(request: Request, token: str):
    settings = settings_of(request)
    with db_conn(request) as conn:
        user_id = consume_verification(conn, settings, token)
        if user_id is None:
            return render(request, "verify_failed.html", auth=None)
        mark_user_verified(conn, user_id)
        user = get_user(conn, user_id)
        if not user or not user["tenant_id"]:
            return render(request, "login.html", auth=None, error=None,
                          notice="Email verified — sign in to your new studio.")
        session = create_session(conn, role=user["role"], user_id=user["id"],
                                 tenant_id=user["tenant_id"])
        target = _owner_home(conn, user["tenant_id"])
    return _session_redirect(settings, session, target)


@router.get("/forgot")
def forgot_form(request: Request):
    return render(request, "forgot.html", auth=None, sent=False)


@router.post("/forgot")
def forgot_submit(request: Request, email: str = Form(...)):
    enforce(request, "password_reset")
    settings = settings_of(request)
    with db_conn(request) as conn:
        user = get_user_by_email(conn, email)
        if user and user["tenant_id"]:
            token = create_reset(conn, settings, user_id=user["id"])
            link = f"{settings.public_url.rstrip('/')}/reset/{token}"
            notify(conn, settings, to=user["email"], tenant_id=user["tenant_id"], signed=False,
                   subject="Reset your Hestia password",
                   body=(f"A password reset was requested for your account.\n\n"
                         f"Reset it here (the link expires in 1 hour):\n{link}\n\n"
                         f"If this wasn't you, you can safely ignore this email."))
    # Identical response whether or not the email matched — no account enumeration.
    return render(request, "forgot.html", auth=None, sent=True)


@router.get("/reset/{token}")
def reset_form(request: Request, token: str):
    with db_conn(request) as conn:
        valid = find_reset(conn, settings_of(request), token) is not None
    return render(request, "reset.html", auth=None, token=token, valid=valid, error=None)


@router.post("/reset/{token}")
def reset_submit(request: Request, token: str, password: str = Form(...),
                 confirm: str = Form("")):
    enforce(request, "password_reset")
    settings = settings_of(request)
    with db_conn(request) as conn:
        if find_reset(conn, settings, token) is None:
            return render(request, "reset.html", auth=None, token=token, valid=False, error=None)
        if len(password) < 8 or password != confirm:
            return render(request, "reset.html", auth=None, token=token, valid=True,
                          error="Passwords must match and be at least 8 characters.")
        user_id = consume_reset(conn, settings, token)
        if user_id is None:  # raced/expired between the check and the burn
            return render(request, "reset.html", auth=None, token=token, valid=False, error=None)
        set_user_password(conn, user_id, password)
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # log out everywhere
    return render(request, "login.html", auth=None, error=None,
                  notice="Password updated — sign in with your new password.")


@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    with db_conn(request) as conn:
        destroy_session(conn, token)
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/dashboard")
def dashboard(request: Request):
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or auth.is_admin or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        flags = tenant_flags(tenant)
        galleries = list_galleries(conn, tenant["id"])[:6]
        runs = list_runs(conn, tenant["id"], limit=6)
        plan = plan_status(tenant)
        unpaid = conn.execute(
            # plan installments are tracked under their plan, not the flat invoice list
            "SELECT COUNT(*) AS n FROM invoices "
            "WHERE tenant_id = ? AND status IN ('draft','sent') AND plan_id IS NULL",
            (tenant["id"],),
        ).fetchone()["n"]
        counts = {
            "clients": len(list_clients(conn, tenant["id"])),
            "projects": len(list_projects(conn, tenant["id"])),
            "galleries": len(list_galleries(conn, tenant["id"])),
            "unpaid": unpaid,
        }
        profile = get_profile(conn, tenant["id"])
        attention = needs_attention(conn, tenant["id"])
        snapshot = money_snapshot(conn, tenant["id"])
        proposal_stats = proposal_metrics(conn, tenant["id"])
        setup = setup_checklist(conn, tenant["id"], published=profile["published"])
        subscription = get_subscription(conn, tenant["id"])
        trial = trial_cockpit(tenant, subscription, settings_of(request), setup)
        reconnect = reconnect_due(conn, tenant["id"])
    return render(request, "dashboard.html", auth=auth, tenant=tenant, flags=flags,
                  galleries=galleries, runs=runs, plan=plan, counts=counts, profile=profile,
                  attention=attention, snapshot=snapshot, setup=setup, trial=trial,
                  reconnect=reconnect, proposal_stats=proposal_stats)


@router.post("/dashboard/digest")
def dashboard_digest_now(request: Request):
    """Send the owner this studio's digest right now (the 'email me this' button) — the
    same summary the worker mails weekly, on demand and ignoring the cooldown."""
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or auth.is_admin or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        result = send_owner_digest_now(conn, settings, auth.tenant["id"])
        if result is not None:
            audit(conn, actor="owner", action="digest.sent", tenant_id=auth.tenant["id"],
                  detail="owner digest emailed")
            conn.commit()
    return RedirectResponse("/dashboard", status_code=303)

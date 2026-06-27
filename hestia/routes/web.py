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
from ..crm import list_clients, list_projects
from ..dashboard import needs_attention
from ..email import notify
from ..galleries import list_galleries
from ..pipeline import list_runs
from ..ratelimit import enforce
from ..resets import consume_reset, create_reset, find_reset
from ..studio import get_profile
from ..tenants import (
    create_tenant,
    create_user,
    get_tenant,
    get_user_by_email,
    mark_user_verified,
    set_user_password,
    tenant_flags,
)
from ..verifications import consume_verification, create_verification
from .deps import db_conn, render, settings_of

router = APIRouter()


@router.get("/")
def landing(request: Request):
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
    return render(request, "landing.html", auth=auth)


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
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=cookie_is_secure(settings), max_age=int(SESSION_TTL.total_seconds()))
    return resp


@router.get("/signup")
def signup_form(request: Request):
    if not settings_of(request).signup_enabled:
        return RedirectResponse("/login", status_code=303)
    return render(request, "signup.html", auth=None, sent=False, error=None)


@router.post("/signup")
def signup_submit(request: Request, name: str = Form(...), email: str = Form(...),
                  password: str = Form(...), shoot_type: str = Form("other")):
    settings = settings_of(request)
    if not settings.signup_enabled:
        return RedirectResponse("/login", status_code=303)
    enforce(request, "signup")

    def _again(error: str):
        return render(request, "signup.html", auth=None, sent=False, error=error)

    with db_conn(request) as conn:
        email_norm = email.strip().lower()
        if len(password) < 8:
            return _again("Choose a password of at least 8 characters.")
        if get_user_by_email(conn, email_norm):
            return _again("That email is already registered — try signing in instead.")
        tenant = create_tenant(conn, name=name, shoot_type=shoot_type)
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
    return render(request, "login.html", auth=None, error=None,
                  notice="Email verified — sign in to your new studio.")


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
    return render(request, "dashboard.html", auth=auth, tenant=tenant, flags=flags,
                  galleries=galleries, runs=runs, plan=plan, counts=counts, profile=profile,
                  attention=attention)

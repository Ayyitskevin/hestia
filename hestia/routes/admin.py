"""Admin UI — invite-only studio management + onboarding wizard.

Admins authenticate with the master ``HESTIA_API_TOKEN``. In Phase 0 the admin
creates studios (tenants), sets shoot type, seeds the owner user, and mints the
studio's ``hestia_tk_*`` API key. No service wiring — it's one app now.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import __version__
from ..auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    context_from_session,
    cookie_is_secure,
    create_session,
    destroy_session,
)
from ..billing import PLANS, plan_status
from ..db import applied_migrations
from ..jobs import queue_stats
from ..ratelimit import enforce
from ..tenants import (
    create_tenant,
    create_tenant_api_key,
    create_user,
    get_tenant,
    list_tenants,
    set_shoot_type,
    tenant_flags,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/admin")


def _is_admin(request: Request, conn) -> bool:
    auth = context_from_session(conn, request)
    return bool(auth and auth.is_admin)


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


@router.get("")
def admin_home(request: Request):
    with db_conn(request) as conn:
        if _is_admin(request, conn):
            return RedirectResponse("/admin/tenants", status_code=303)
    return render(request, "admin/login.html", auth=None, error=None)


@router.post("/login")
def admin_login(request: Request, token: str = Form(...)):
    enforce(request, "admin_login")
    settings = settings_of(request)
    if not settings.api_token or not hmac.compare_digest(token, settings.api_token):
        return render(request, "admin/login.html", auth=None, error="Invalid admin token.")
    with db_conn(request) as conn:
        session_token = create_session(conn, role="admin")
    resp = RedirectResponse("/admin/tenants", status_code=303)
    resp.set_cookie(SESSION_COOKIE, session_token, httponly=True, samesite="lax",
                    secure=cookie_is_secure(settings), max_age=int(SESSION_TTL.total_seconds()))
    return resp


@router.get("/logout")
def admin_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    with db_conn(request) as conn:
        destroy_session(conn, token)
    resp = RedirectResponse("/admin", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/tenants")
def tenants_list(request: Request):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        tenants = list_tenants(conn)
    return render(request, "admin/tenants.html", auth=None, tenants=tenants)


@router.get("/system")
def system(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        info = {
            "version": __version__,
            "tenants": len(list_tenants(conn)),
            "queue": queue_stats(conn),
            "migrations": applied_migrations(conn),
            "seams": {
                "vision": settings.vision_backend,
                "album": settings.album_backend,
                "content": settings.content_backend,
                "product": settings.product_backend,
                "storage": settings.storage_backend,
                "payments": settings.payments_backend,
                "subscription": settings.subscription_backend,
                "email": settings.email_backend,
            },
            "log_format": settings.log_format,
            "signup_enabled": settings.signup_enabled,
            "insecure_secrets": settings.insecure_secrets,
        }
    return render(request, "admin/system.html", auth=None, info=info)


@router.get("/onboarding")
def onboarding_form(request: Request):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
    return render(request, "admin/onboarding.html", auth=None)


@router.post("/onboarding")
def onboarding_submit(
    request: Request,
    name: str = Form(...),
    shoot_type: str = Form("other"),
    owner_email: str = Form(...),
    owner_password: str = Form(...),
):
    settings = settings_of(request)
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        tenant = create_tenant(conn, name=name, shoot_type=shoot_type)
        create_user(conn, tenant_id=tenant["id"], email=owner_email,
                    password=owner_password, role="owner")
        api_key = create_tenant_api_key(conn, settings, tenant["id"])
        tenant = get_tenant(conn, tenant["id"])
    return _render_tenant_detail(request, tenant["id"], new_api_key=api_key, created=True)


@router.get("/tenants/{tenant_id}")
def tenant_detail(request: Request, tenant_id: str):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
    return _render_tenant_detail(request, tenant_id)


@router.post("/tenants/{tenant_id}/shoot-type")
def update_shoot_type(request: Request, tenant_id: str, shoot_type: str = Form(...)):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        set_shoot_type(conn, tenant_id, shoot_type)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/api-key")
def mint_api_key(request: Request, tenant_id: str):
    settings = settings_of(request)
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        api_key = create_tenant_api_key(conn, settings, tenant_id)
    return _render_tenant_detail(request, tenant_id, new_api_key=api_key)


def _render_tenant_detail(request: Request, tenant_id: str, *,
                          new_api_key: str | None = None, created: bool = False):
    with db_conn(request) as conn:
        tenant = get_tenant(conn, tenant_id)
        if not tenant:
            return RedirectResponse("/admin/tenants", status_code=303)
        flags = tenant_flags(tenant)
        plan = plan_status(tenant)
        api_keys = conn.execute(
            "SELECT prefix, created_at FROM tenant_api_keys WHERE tenant_id = ? ORDER BY id DESC",
            (tenant_id,),
        ).fetchall()
    return render(request, "admin/tenant_detail.html", auth=None, tenant=tenant, flags=flags,
                  plan=plan, plans=PLANS, new_api_key=new_api_key, created=created,
                  api_keys=[dict(r) for r in api_keys])

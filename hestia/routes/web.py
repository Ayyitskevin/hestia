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
from ..galleries import list_galleries
from ..pipeline import list_runs
from ..tenants import get_tenant, tenant_flags
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
    settings = settings_of(request)
    with db_conn(request) as conn:
        user = authenticate_user(conn, email, password)
        if not user or not user["tenant_id"]:
            return render(request, "login.html", auth=None, error="Invalid email or password.")
        token = create_session(conn, role=user["role"], user_id=user["id"],
                               tenant_id=user["tenant_id"])
    resp = RedirectResponse("/dashboard", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=cookie_is_secure(settings), max_age=int(SESSION_TTL.total_seconds()))
    return resp


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
        counts = {
            "clients": len(list_clients(conn, tenant["id"])),
            "projects": len(list_projects(conn, tenant["id"])),
            "galleries": len(list_galleries(conn, tenant["id"])),
        }
    return render(request, "dashboard.html", auth=auth, tenant=tenant, flags=flags,
                  galleries=galleries, runs=runs, plan=plan, counts=counts)

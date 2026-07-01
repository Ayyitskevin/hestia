"""Admin UI — invite-only studio management + onboarding wizard.

Admins authenticate with the master ``HESTIA_API_TOKEN``. In Phase 0 the admin
creates studios (tenants), sets shoot type, seeds the owner user, and mints the
studio's ``hestia_tk_*`` API key. No service wiring — it's one app now.
"""

from __future__ import annotations

import csv
import hmac
import io

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

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
from ..db import applied_migrations, audit
from ..domains import custom_domain_summary, set_custom_domain_status
from ..integrity import tenant_integrity_overview
from ..interest import send_beta_interest_invite
from ..jobs import failed_jobs, queue_stats, requeue_job, stale_jobs
from ..launch import beta_launch_export_rows, beta_launch_kit, send_beta_launch_nudge
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
from ..trial_conversion import (
    beta_conversion_timeline,
    trial_conversion_cockpit,
    trial_conversion_for_tenant,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/admin")

_LAUNCH_EXPORT_HEADER = [
    "studio",
    "slug",
    "owner_email",
    "owner_verified",
    "source",
    "landing_path",
    "trial_state",
    "trial_label",
    "risk",
    "risk_reason",
    "activation",
    "activation_percent",
    "next_action",
    "owner_path",
    "followup_prompt",
    "mailto",
    "last_nudged_at",
    "nudge_status",
]


def _is_admin(request: Request, conn) -> bool:
    auth = context_from_session(conn, request)
    return bool(auth and auth.is_admin)


def _admin_ctx(request: Request, conn):
    """The admin's AuthContext, or None. Passed to ``render`` so the operator nav
    (Studios / System / Sign out) actually appears — admin pages used to render with
    ``auth=None``, leaving the surface reachable only by typing URLs."""
    auth = context_from_session(conn, request)
    return auth if (auth and auth.is_admin) else None


def _redirect_login() -> RedirectResponse:
    return RedirectResponse("/admin", status_code=303)


def _csv_safe(value) -> str:
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _csv_response(filename: str, rows: list[dict]) -> Response:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_LAUNCH_EXPORT_HEADER)
    for row in rows:
        writer.writerow([_csv_safe(row.get(key, "")) for key in _LAUNCH_EXPORT_HEADER])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


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
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        tenants = list_tenants(conn)
    return render(request, "admin/tenants.html", auth=auth, tenants=tenants)


@router.get("/trials")
def trials(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        cockpit = trial_conversion_cockpit(conn, settings)
    return render(request, "admin/trials.html", auth=auth, cockpit=cockpit)


@router.get("/launch")
def launch(request: Request, nudge: str = "", nudged: str = "", interest: str = ""):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        kit = beta_launch_kit(conn, settings)
    nudge_notice = nudge or ("sent" if nudged else "")
    return render(
        request,
        "admin/launch.html",
        auth=auth,
        kit=kit,
        nudge_notice=nudge_notice,
        interest_notice=interest,
    )


@router.get("/launch/export.csv")
def launch_export(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        rows = beta_launch_export_rows(conn, settings)
    return _csv_response("hestia-beta-launch.csv", rows)


@router.post("/launch/{tenant_id}/nudge")
def launch_nudge(request: Request, tenant_id: str):
    settings = settings_of(request)
    nudge_status = "missing"
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        result = send_beta_launch_nudge(conn, settings, tenant_id)
        if result and result.get("skipped"):
            nudge_status = "cooldown"
            audit(
                conn,
                actor="admin",
                action="launch.nudge_skipped",
                tenant_id=tenant_id,
                detail=f"cooldown:{result['owner_email']}",
            )
        elif result:
            nudge_status = "sent"
            audit(
                conn,
                actor="admin",
                action="launch.nudge_sent",
                tenant_id=tenant_id,
                detail=result["owner_email"],
            )
    return RedirectResponse(f"/admin/launch?nudge={nudge_status}", status_code=303)


@router.post("/launch/interest/{interest_id}/invite")
def launch_interest_invite(request: Request, interest_id: int):
    settings = settings_of(request)
    invite_status = "missing"
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        result = send_beta_interest_invite(conn, settings, interest_id)
        if result and result.get("skipped"):
            invite_status = "converted"
            audit(
                conn,
                actor="admin",
                action="interest.invite_skipped",
                detail=f"converted:{result['email']}",
            )
        elif result:
            invite_status = "sent"
            audit(
                conn,
                actor="admin",
                action="interest.invite_sent",
                detail=result["email"],
            )
    return RedirectResponse(f"/admin/launch?interest={invite_status}", status_code=303)


@router.get("/system")
def system(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        info = {
            "version": __version__,
            "tenants": len(list_tenants(conn)),
            "queue": queue_stats(conn),
            "failed": failed_jobs(conn),
            "stale": stale_jobs(conn),
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
                "fulfillment": settings.fulfillment_backend,
            },
            "log_format": settings.log_format,
            "signup_enabled": settings.signup_enabled,
            "warnings": settings.config_warnings,
        }
    return render(request, "admin/system.html", auth=auth, info=info)


@router.get("/integrity")
def integrity(request: Request):
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
        overview = tenant_integrity_overview(conn)
    return render(request, "admin/integrity.html", auth=auth, overview=overview)


@router.post("/system/jobs/{job_id}/requeue")
def requeue(request: Request, job_id: int):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        moved = requeue_job(conn, job_id)
        if moved:
            audit(conn, actor="admin", action="job.requeued", detail=str(job_id))
    return RedirectResponse("/admin/system", status_code=303)


@router.get("/onboarding")
def onboarding_form(request: Request):
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        if not auth:
            return _redirect_login()
    return render(request, "admin/onboarding.html", auth=auth)


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


@router.post("/tenants/{tenant_id}/custom-domain/verify")
def verify_custom_domain(request: Request, tenant_id: str):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        try:
            set_custom_domain_status(conn, tenant_id, "verified")
        except ValueError:
            return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)
        audit(conn, actor="admin", action="custom_domain.verified", tenant_id=tenant_id)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


@router.post("/tenants/{tenant_id}/custom-domain/pending")
def reset_custom_domain(request: Request, tenant_id: str):
    with db_conn(request) as conn:
        if not _is_admin(request, conn):
            return _redirect_login()
        try:
            set_custom_domain_status(conn, tenant_id, "pending")
        except ValueError:
            return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)
        audit(conn, actor="admin", action="custom_domain.pending", tenant_id=tenant_id)
    return RedirectResponse(f"/admin/tenants/{tenant_id}", status_code=303)


def _render_tenant_detail(request: Request, tenant_id: str, *,
                          new_api_key: str | None = None, created: bool = False):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _admin_ctx(request, conn)
        tenant = get_tenant(conn, tenant_id)
        if not tenant:
            return RedirectResponse("/admin/tenants", status_code=303)
        flags = tenant_flags(tenant)
        plan = plan_status(tenant)
        conversion = trial_conversion_for_tenant(conn, tenant, settings)
        conversion_timeline = beta_conversion_timeline(
            conn,
            tenant,
            settings,
            conversion=conversion,
        )
        api_keys = conn.execute(
            "SELECT prefix, created_at FROM tenant_api_keys WHERE tenant_id = ? ORDER BY id DESC",
            (tenant_id,),
        ).fetchall()
    return render(request, "admin/tenant_detail.html", auth=auth, tenant=tenant, flags=flags,
                  plan=plan, plans=PLANS, new_api_key=new_api_key, created=created,
                  api_keys=[dict(r) for r in api_keys],
                  custom_domain=custom_domain_summary(settings, tenant),
                  conversion=conversion,
                  conversion_timeline=conversion_timeline)

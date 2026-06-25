"""Studio site routes — public marketing page + inquiry intake + owner settings."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..ratelimit import enforce
from ..studio import create_inquiry, get_profile, upsert_profile
from ..tenants import get_tenant, get_tenant_by_slug
from .deps import db_conn, render

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


# ── Public studio site ──────────────────────────────────────────────────────


@router.get("/studio/{slug}")
def public_site(request: Request, slug: str):
    with db_conn(request) as conn:
        tenant = get_tenant_by_slug(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        profile = get_profile(conn, tenant["id"])
    if not profile["published"]:
        return render(request, "studio/coming_soon.html", auth=None, tenant=tenant)
    return render(request, "studio/site.html", auth=None, tenant=tenant, profile=profile)


@router.post("/studio/{slug}/inquire")
def public_inquire(request: Request, slug: str, name: str = Form(...), email: str = Form(""),
                   message: str = Form(""), shoot_type: str = Form("other"),
                   event_date: str = Form("")):
    enforce(request, "inquiry")
    with db_conn(request) as conn:
        tenant = get_tenant_by_slug(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        profile = get_profile(conn, tenant["id"])
        if not profile["published"]:
            return render(request, "studio/coming_soon.html", auth=None, tenant=tenant)
        create_inquiry(conn, tenant=tenant, name=name, email=email, message=message,
                       shoot_type=shoot_type, event_date=event_date)
    return render(request, "studio/thanks.html", auth=None, tenant=tenant)


# ── Owner site settings ─────────────────────────────────────────────────────


@router.get("/settings/site")
def site_settings(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        profile = get_profile(conn, tenant["id"])
    return render(request, "studio/settings.html", auth=auth, tenant=tenant, profile=profile)


@router.post("/settings/site")
def site_settings_save(request: Request, headline: str = Form(""), about: str = Form(""),
                       contact_email: str = Form(""), published: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        upsert_profile(conn, tenant_id=auth.tenant["id"], headline=headline, about=about,
                       contact_email=contact_email, published=bool(published))
    return RedirectResponse("/settings/site", status_code=303)

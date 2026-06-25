"""Studio site routes — public marketing page + inquiry intake + owner settings."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..email import list_emails, notify
from ..ratelimit import enforce
from ..studio import create_inquiry, get_profile, upsert_profile
from ..tenants import get_tenant, get_tenant_by_slug
from .deps import db_conn, render, settings_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _studio_inbox(conn, tenant_id: str, profile: dict) -> str:
    """Where lead alerts go: the studio's stated contact, else the owner's login."""
    if profile.get("contact_email"):
        return profile["contact_email"]
    row = conn.execute(
        "SELECT email FROM users WHERE tenant_id = ? AND role = 'owner' ORDER BY id LIMIT 1",
        (tenant_id,),
    ).fetchone()
    return row["email"] if row else ""


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
        # Alert the studio that a lead came in (mock records it; smtp also sends).
        inbox = _studio_inbox(conn, tenant["id"], profile)
        notify(conn, settings_of(request), to=inbox, tenant_id=tenant["id"],
               subject=f"New {shoot_type} inquiry from {name or email or 'website'}",
               body=(f"{name or 'Someone'} just inquired via your studio site.\n\n"
                     f"Email: {email or '—'}\nShoot type: {shoot_type}\n"
                     f"Event date: {event_date or '—'}\n\nMessage:\n{message or '(none)'}\n\n"
                     f"They're already in your CRM as a new lead."))
        conn.commit()
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


@router.get("/settings/outbox")
def outbox(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        emails = list_emails(conn, auth.tenant["id"])
    return render(request, "studio/outbox.html", auth=auth, emails=emails,
                  email_backend=settings.email_backend)

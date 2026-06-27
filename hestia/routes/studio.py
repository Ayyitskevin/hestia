"""Studio site routes — public marketing page + inquiry intake + owner settings."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import messaging
from ..auth import context_from_session
from ..db import list_audit
from ..email import list_emails, notify
from ..ratelimit import enforce
from ..referrals import attribute_referral
from ..studio import create_inquiry, get_profile, upsert_profile
from ..tenants import (
    can_use_style_profile,
    get_tenant,
    get_tenant_by_slug,
    set_email_signature,
    set_tax_rate,
    set_vision_style,
)
from ..testimonials import featured_testimonials
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
def public_site(request: Request, slug: str, ref: str = ""):
    with db_conn(request) as conn:
        tenant = get_tenant_by_slug(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        profile = get_profile(conn, tenant["id"])
        if not profile["published"]:
            return render(request, "studio/coming_soon.html", auth=None, tenant=tenant)
        testimonials = featured_testimonials(conn, tenant["id"])
    return render(request, "studio/site.html", auth=None, tenant=tenant, profile=profile,
                  testimonials=testimonials, ref=ref)


@router.post("/studio/{slug}/inquire")
def public_inquire(request: Request, slug: str, name: str = Form(...), email: str = Form(""),
                   message: str = Form(""), shoot_type: str = Form("other"),
                   event_date: str = Form(""), ref: str = Form("")):
    enforce(request, "inquiry")
    with db_conn(request) as conn:
        tenant = get_tenant_by_slug(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        profile = get_profile(conn, tenant["id"])
        if not profile["published"]:
            return render(request, "studio/coming_soon.html", auth=None, tenant=tenant)
        project = create_inquiry(conn, tenant=tenant, name=name, email=email, message=message,
                                 shoot_type=shoot_type, event_date=event_date)
        attribute_referral(conn, tenant["id"], project["id"], ref)
        # Alert the studio that a lead came in (mock records it; smtp also sends).
        inbox = _studio_inbox(conn, tenant["id"], profile)
        notify(conn, settings_of(request), to=inbox, tenant_id=tenant["id"], signed=False,
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
    return render(request, "studio/settings.html", auth=auth, tenant=tenant, profile=profile,
                  can_style=can_use_style_profile(tenant))


@router.post("/settings/vision-style")
def vision_style_save(request: Request, vision_style: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        if can_use_style_profile(tenant):  # tier gate enforced server-side
            set_vision_style(conn, auth.tenant["id"], vision_style)
    return RedirectResponse("/settings/site", status_code=303)


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


@router.post("/settings/tax")
def tax_settings_save(request: Request, tax_rate: str = Form("0")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        try:
            pct = float(tax_rate)                       # a percentage, e.g. 8.5
            bps = round(pct * 100) if math.isfinite(pct) else 0
        except (TypeError, ValueError):
            bps = 0
        set_tax_rate(conn, auth.tenant["id"], bps)
    return RedirectResponse("/settings/site", status_code=303)


@router.post("/settings/signature")
def signature_save(request: Request, email_signature: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_email_signature(conn, auth.tenant["id"], email_signature)
    return RedirectResponse("/settings/site", status_code=303)


@router.get("/settings/messages")
def message_templates(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        templates = messaging.list_templates(conn, auth.tenant["id"],
                                             studio=auth.tenant.get("name") or "")
    return render(request, "studio/messages.html", auth=auth, templates=templates)


@router.post("/settings/messages/{kind}")
def message_template_save(request: Request, kind: str, subject: str = Form(""),
                          body: str = Form("")):
    """Save a studio's custom email template; blanking both fields resets to default."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        messaging.set_template(conn, auth.tenant["id"], kind, subject=subject, body=body)
    return RedirectResponse("/settings/messages", status_code=303)


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


@router.get("/settings/activity")
def activity(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        events = list_audit(conn, auth.tenant["id"])
    return render(request, "studio/activity.html", auth=auth, events=events)

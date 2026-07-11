"""Mini-session drop routes — owner setup and public slot claims."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..dashboard import owner_digest_recipient
from ..email import notify
from ..mini_sessions import (
    add_mini_session_slots,
    claim_mini_session_slot,
    create_mini_session,
    delete_open_slot,
    get_mini_session,
    get_mini_session_by_slug,
    hydrate_mini_session_displays,
    list_mini_session_slots,
    list_mini_sessions,
    set_mini_session_status,
)
from ..ratelimit import enforce
from ..scheduler import appointment_ics_url
from ..studio import get_profile
from ..tenants import get_tenant_by_slug
from .deps import db_conn, render, settings_of, tenant_user

router = APIRouter()




def _to_cents(raw: str) -> int:
    try:
        cents = float(raw.replace("$", "").replace(",", "").strip()) * 100
        return int(round(cents)) if math.isfinite(cents) else 0
    except (ValueError, AttributeError, OverflowError):
        return 0


def _published_tenant(conn, slug: str):
    tenant = get_tenant_by_slug(conn, slug)
    if not tenant:
        return None, None
    profile = get_profile(conn, tenant["id"])
    return (tenant, profile) if profile["published"] else (None, None)


@router.get("/mini-sessions")
def mini_sessions_list(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        drops = hydrate_mini_session_displays(
            settings,
            auth.tenant["slug"],
            list_mini_sessions(conn, auth.tenant["id"]),
        )
    return render(request, "mini_sessions/list.html", auth=auth, drops=drops)


@router.post("/mini-sessions")
def mini_session_create(
    request: Request,
    title: str = Form(...),
    description: str = Form(""),
    duration_minutes: str = Form("20"),
    price: str = Form("0"),
    deposit: str = Form("0"),
):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        duration = int(duration_minutes) if duration_minutes.strip().isdigit() else 20
        drop = create_mini_session(
            conn,
            tenant_id=auth.tenant["id"],
            title=title,
            description=description,
            duration_minutes=duration,
            price_cents=_to_cents(price),
            deposit_cents=_to_cents(deposit),
        )
    return RedirectResponse(f"/mini-sessions/{drop['id']}" if drop else "/mini-sessions", status_code=303)


@router.get("/mini-sessions/{session_id}")
def mini_session_detail(request: Request, session_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        drop = get_mini_session(conn, auth.tenant["id"], session_id)
        if not drop:
            return RedirectResponse("/mini-sessions", status_code=303)
        hydrate_mini_session_displays(settings, auth.tenant["slug"], [drop])
        slots = list_mini_session_slots(conn, auth.tenant["id"], drop["id"])
    return render(request, "mini_sessions/detail.html", auth=auth, drop=drop, slots=slots)


@router.post("/mini-sessions/{session_id}/slots")
def mini_session_add_slots(request: Request, session_id: int, starts_at: str = Form("")):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        add_mini_session_slots(conn, auth.tenant["id"], session_id, starts_at)
    return RedirectResponse(f"/mini-sessions/{session_id}", status_code=303)


@router.post("/mini-sessions/{session_id}/publish")
def mini_session_publish(request: Request, session_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_mini_session_status(conn, auth.tenant["id"], session_id, "published")
    return RedirectResponse(f"/mini-sessions/{session_id}", status_code=303)


@router.post("/mini-sessions/{session_id}/unpublish")
def mini_session_unpublish(request: Request, session_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_mini_session_status(conn, auth.tenant["id"], session_id, "draft")
    return RedirectResponse(f"/mini-sessions/{session_id}", status_code=303)


@router.post("/mini-sessions/{session_id}/archive")
def mini_session_archive(request: Request, session_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_mini_session_status(conn, auth.tenant["id"], session_id, "archived")
    return RedirectResponse("/mini-sessions", status_code=303)


@router.post("/mini-sessions/{session_id}/slots/{slot_id}/delete")
def mini_session_delete_slot(request: Request, session_id: int, slot_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_open_slot(conn, auth.tenant["id"], slot_id)
    return RedirectResponse(f"/mini-sessions/{session_id}", status_code=303)


@router.get("/studio/{slug}/mini-sessions/{drop_slug}")
def public_mini_session_page(request: Request, slug: str, drop_slug: str):
    settings = settings_of(request)
    with db_conn(request) as conn:
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            t = get_tenant_by_slug(conn, slug)
            if t:
                return render(request, "studio/coming_soon.html", auth=None, tenant=t)
            return render(request, "offer_missing.html", auth=None, status_code=404)
        drop = get_mini_session_by_slug(conn, tenant["id"], drop_slug)
        if not drop or drop["status"] != "published":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        hydrate_mini_session_displays(settings, tenant["slug"], [drop])
        slots = list_mini_session_slots(conn, tenant["id"], drop["id"], public_only=True)
    return render(request, "mini_sessions/public.html", auth=None, tenant=tenant,
                  profile=profile, drop=drop, slots=slots)


@router.post("/studio/{slug}/mini-sessions/{drop_slug}")
def public_mini_session_claim(
    request: Request,
    slug: str,
    drop_slug: str,
    slot_id: str = Form(""),
    name: str = Form(""),
    email: str = Form(""),
    message: str = Form(""),
):
    enforce(request, "inquiry")
    settings = settings_of(request)
    with db_conn(request) as conn:
        conn.execute("BEGIN IMMEDIATE")
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        drop = get_mini_session_by_slug(conn, tenant["id"], drop_slug)
        if not drop or drop["status"] != "published":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        hydrate_mini_session_displays(settings, tenant["slug"], [drop])

        def _re_render(err: str):
            slots = list_mini_session_slots(conn, tenant["id"], drop["id"], public_only=True)
            return render(request, "mini_sessions/public.html", auth=None, tenant=tenant,
                          profile=profile, drop=drop, slots=slots, error=err, status_code=400)

        if not (name or "").strip():
            return _re_render("Please tell us your name.")
        if not (email or "").strip():
            return _re_render("Please add your email so we can confirm your spot.")
        if not slot_id.strip().isdigit():
            return _re_render("Please choose an open spot.")

        result = claim_mini_session_slot(
            conn,
            settings,
            tenant=tenant,
            drop=drop,
            slot_id=int(slot_id),
            name=name,
            email=email,
            message=message,
        )
        if not result:
            return _re_render("That spot was just claimed — please choose another.")

        inbox = owner_digest_recipient(conn, tenant["id"])
        if inbox:
            notify(
                conn,
                settings,
                to=inbox,
                tenant_id=tenant["id"],
                signed=False,
                subject=f"Mini-session booked: {drop['title']}",
                body=(
                    f"{name.strip()} claimed a mini-session spot.\n\n"
                    f"Drop: {drop['title']}\n"
                    f"Time: {result['slot']['starts_at']}\n"
                    f"Email: {email.strip()}\n"
                    + (f"\nMessage:\n{message.strip()}\n" if message.strip() else "")
                    + "\nThey're already in your CRM and schedule."
                ),
            )
        conn.commit()
        invoice = result["invoice"]
    if invoice:
        return RedirectResponse(f"/pay/{invoice['token']}", status_code=303)
    calendar_url = appointment_ics_url(settings, result["appointment"]["token"])
    return render(request, "studio/book_thanks.html", auth=None, tenant=tenant,
                  booking_title=drop["title"], confirmed=True, calendar_url=calendar_url)

"""Self-serve booking routes — owner manages the session-type menu; the public books.

Owner side (``/settings/booking-types``, session + CSRF): CRUD over the studio's
bookable session types. Public side (``/studio/{slug}/book``, cookieless → CSRF-exempt
like the inquiry form): a visitor picks a published type, requests a time, and lands in
the CRM as a lead + a proposed appointment the owner confirms.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..booking import (
    create_booking_type,
    delete_booking_type,
    list_booking_types,
    request_booking,
    set_booking_type_active,
)
from ..dashboard import owner_digest_recipient
from ..email import notify
from ..invoices import money
from ..ratelimit import enforce
from ..scheduler import APPOINTMENT_KINDS, KIND_LABELS
from ..studio import get_profile
from ..tenants import get_tenant_by_slug
from .deps import db_conn, render, settings_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    try:
        cents = float(raw.replace("$", "").replace(",", "").strip()) * 100
        return int(round(cents)) if math.isfinite(cents) else 0
    except (ValueError, AttributeError, OverflowError):
        return 0


def _kind_choices() -> list[dict]:
    return [{"value": k, "label": KIND_LABELS.get(k, k)} for k in APPOINTMENT_KINDS]


# ── Owner: manage the session-type menu ──────────────────────────────────────


@router.get("/settings/booking-types")
def booking_types_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        currency = settings_of(request).currency
        types = list_booking_types(conn, auth.tenant["id"])
        for t in types:
            t["price_display"] = money(t["price_cents"], currency) if t["price_cents"] else ""
            t["kind_label"] = KIND_LABELS.get(t["kind"], t["kind"])
        profile = get_profile(conn, auth.tenant["id"])
    return render(request, "studio/booking_types.html", auth=auth, types=types,
                  kinds=_kind_choices(), published=bool(profile["published"]),
                  slug=auth.tenant.get("slug", ""))


@router.post("/settings/booking-types")
def booking_type_create(request: Request, title: str = Form(...), description: str = Form(""),
                        kind: str = Form("consultation"), duration_minutes: str = Form("60"),
                        price: str = Form("0")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        dur = int(duration_minutes) if duration_minutes.strip().isdigit() else 60
        create_booking_type(conn, tenant_id=auth.tenant["id"], title=title, description=description,
                            kind=kind, duration_minutes=dur, price_cents=_to_cents(price))
    return RedirectResponse("/settings/booking-types", status_code=303)


@router.post("/settings/booking-types/{type_id}/toggle")
def booking_type_toggle(request: Request, type_id: int, active: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_booking_type_active(conn, auth.tenant["id"], type_id, bool(active.strip()))
    return RedirectResponse("/settings/booking-types", status_code=303)


@router.post("/settings/booking-types/{type_id}/delete")
def booking_type_delete(request: Request, type_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_booking_type(conn, auth.tenant["id"], type_id)
    return RedirectResponse("/settings/booking-types", status_code=303)


# ── Public: the "book me" page ────────────────────────────────────────────────


def _published_tenant(conn, slug: str):
    """The published studio for ``slug``, or None — the public booking gate."""
    tenant = get_tenant_by_slug(conn, slug)
    if not tenant:
        return None, None
    profile = get_profile(conn, tenant["id"])
    return (tenant, profile) if profile["published"] else (None, None)


@router.get("/studio/{slug}/book")
def public_book_page(request: Request, slug: str):
    with db_conn(request) as conn:
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            t = get_tenant_by_slug(conn, slug)
            if t:
                return render(request, "studio/coming_soon.html", auth=None, tenant=t)
            return render(request, "offer_missing.html", auth=None, status_code=404)
        currency = settings_of(request).currency
        types = list_booking_types(conn, tenant["id"], active_only=True)
        for t in types:
            t["price_display"] = money(t["price_cents"], currency) if t["price_cents"] else ""
    return render(request, "studio/book.html", auth=None, tenant=tenant, profile=profile, types=types)


@router.post("/studio/{slug}/book")
def public_book_submit(request: Request, slug: str, booking_type_id: str = Form(""),
                       name: str = Form(""), email: str = Form(""), requested_at: str = Form(""),
                       message: str = Form("")):
    enforce(request, "inquiry")
    with db_conn(request) as conn:
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        currency = settings_of(request).currency
        active = list_booking_types(conn, tenant["id"], active_only=True)
        chosen = next((t for t in active if str(t["id"]) == booking_type_id.strip()), None)
        # re-render the page with an error rather than 500 on a missing type / blank name
        if not chosen or not (name or "").strip():
            for t in active:
                t["price_display"] = money(t["price_cents"], currency) if t["price_cents"] else ""
            err = "Please choose a session type." if not chosen else "Please tell us your name."
            return render(request, "studio/book.html", auth=None, tenant=tenant, profile=profile,
                          types=active, error=err, status_code=400)
        request_booking(conn, tenant=tenant, booking_type=chosen, name=name, email=email,
                        requested_at=requested_at, message=message)
        when = (requested_at or "").replace("T", " ").strip() or "no specific time given"
        inbox = owner_digest_recipient(conn, tenant["id"])
        if inbox:
            notify(conn, settings_of(request), to=inbox, tenant_id=tenant["id"], signed=False,
                   subject=f"New booking request: {chosen['title']}",
                   body=(f"{name.strip() or email or 'Someone'} requested a session via your "
                         f"booking page.\n\nSession: {chosen['title']}\nRequested time: {when}\n"
                         f"Email: {email or '—'}\n"
                         + (f"\nMessage:\n{message.strip()}\n" if (message or '').strip() else "")
                         + "\nThey're in your CRM as a new lead with a proposed session — open it "
                         "in your schedule to confirm the time."))
        conn.commit()
    return render(request, "studio/book_thanks.html", auth=None, tenant=tenant,
                  booking_title=chosen["title"])

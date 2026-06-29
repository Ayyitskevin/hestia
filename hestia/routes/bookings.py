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
from ..availability import (
    WEEKDAYS,
    add_window,
    available_slots,
    delete_window,
    has_availability,
    hhmm_to_minutes,
    is_slot_open,
    list_windows,
)
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
from ..tenants import get_tenant, get_tenant_by_slug, set_booking_rules
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
            t["deposit_display"] = money(t["deposit_cents"], currency) if t["deposit_cents"] else ""
            t["kind_label"] = KIND_LABELS.get(t["kind"], t["kind"])
        profile = get_profile(conn, auth.tenant["id"])
        windows = list_windows(conn, auth.tenant["id"])
        tenant = get_tenant(conn, auth.tenant["id"])
    return render(request, "studio/booking_types.html", auth=auth, types=types,
                  kinds=_kind_choices(), published=bool(profile["published"]),
                  slug=auth.tenant.get("slug", ""), windows=windows,
                  weekdays=list(enumerate(WEEKDAYS)),
                  min_notice_hours=tenant.get("booking_min_notice_hours", 0) if tenant else 0,
                  buffer_minutes=tenant.get("booking_buffer_minutes", 0) if tenant else 0)


@router.post("/settings/booking-types")
def booking_type_create(request: Request, title: str = Form(...), description: str = Form(""),
                        kind: str = Form("consultation"), duration_minutes: str = Form("60"),
                        price: str = Form("0"), deposit: str = Form("0")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        dur = int(duration_minutes) if duration_minutes.strip().isdigit() else 60
        create_booking_type(conn, tenant_id=auth.tenant["id"], title=title, description=description,
                            kind=kind, duration_minutes=dur, price_cents=_to_cents(price),
                            deposit_cents=_to_cents(deposit))
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


@router.post("/settings/availability")
def availability_add(request: Request, weekday: str = Form(""), start: str = Form(""),
                     end: str = Form("")):
    """Add a weekly open-hours window. A malformed weekday/time is a no-op (add_window
    validates), so a bad submit just redirects back without a row."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        wd = int(weekday) if weekday.strip().isdigit() else -1
        start_min, end_min = hhmm_to_minutes(start), hhmm_to_minutes(end)
        if 0 <= wd <= 6 and start_min is not None and end_min is not None:
            add_window(conn, tenant_id=auth.tenant["id"], weekday=wd,
                       start_minute=start_min, end_minute=end_min)
    return RedirectResponse("/settings/booking-types", status_code=303)


@router.post("/settings/availability/{window_id}/delete")
def availability_delete(request: Request, window_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_window(conn, auth.tenant["id"], window_id)
    return RedirectResponse("/settings/booking-types", status_code=303)


@router.post("/settings/booking-rules")
def booking_rules_save(request: Request, min_notice_hours: str = Form("0"),
                       buffer_minutes: str = Form("0")):
    """Save the booking guardrails (minimum notice + buffer). Non-numeric input → 0."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        notice = int(min_notice_hours) if min_notice_hours.strip().isdigit() else 0
        buf = int(buffer_minutes) if buffer_minutes.strip().isdigit() else 0
        set_booking_rules(conn, auth.tenant["id"], min_notice_hours=notice, buffer_minutes=buf)
    return RedirectResponse("/settings/booking-types", status_code=303)


# ── Public: the "book me" page ────────────────────────────────────────────────


def _published_tenant(conn, slug: str):
    """The published studio for ``slug``, or None — the public booking gate."""
    tenant = get_tenant_by_slug(conn, slug)
    if not tenant:
        return None, None
    profile = get_profile(conn, tenant["id"])
    return (tenant, profile) if profile["published"] else (None, None)


def _hydrate(types: list[dict], currency: str) -> list[dict]:
    for t in types:
        t["price_display"] = money(t["price_cents"], currency) if t["price_cents"] else ""
        t["deposit_display"] = money(t["deposit_cents"], currency) if t["deposit_cents"] else ""
    return types


@router.get("/studio/{slug}/book")
def public_book_page(request: Request, slug: str, type: str = ""):
    """Two steps on one route: with no ?type, show the session-type picker; with a valid
    ?type, show that session's booking form — real open slots when the studio has set
    availability, otherwise a free-text time request."""
    currency = settings_of(request).currency
    with db_conn(request) as conn:
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            t = get_tenant_by_slug(conn, slug)
            if t:
                return render(request, "studio/coming_soon.html", auth=None, tenant=t)
            return render(request, "offer_missing.html", auth=None, status_code=404)
        types = _hydrate(list_booking_types(conn, tenant["id"], active_only=True), currency)
        chosen = next((t for t in types if str(t["id"]) == type.strip()), None) if type.strip() else None
        use_slots = bool(chosen and has_availability(conn, tenant["id"]))
        slots = (available_slots(conn, tenant["id"], duration_minutes=chosen["duration_minutes"])
                 if use_slots else [])
    return render(request, "studio/book.html", auth=None, tenant=tenant, profile=profile,
                  types=types, chosen=chosen, slots=slots, use_slots=use_slots)


@router.post("/studio/{slug}/book")
def public_book_submit(request: Request, slug: str, booking_type_id: str = Form(""),
                       name: str = Form(""), email: str = Form(""), requested_at: str = Form(""),
                       slot: str = Form(""), message: str = Form("")):
    enforce(request, "inquiry")
    settings = settings_of(request)
    with db_conn(request) as conn:
        # Take SQLite's write lock up front so the availability re-check (is_slot_open) and the
        # confirming insert are atomic against another concurrent booking — without this, two
        # requests could both pass the check before either commits and double-book one slot.
        conn.execute("BEGIN IMMEDIATE")
        tenant, profile = _published_tenant(conn, slug)
        if not tenant:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        active = _hydrate(list_booking_types(conn, tenant["id"], active_only=True), settings.currency)
        chosen = next((t for t in active if str(t["id"]) == booking_type_id.strip()), None)

        def _re_render(err: str):
            use_slots = bool(chosen and has_availability(conn, tenant["id"]))
            slots = (available_slots(conn, tenant["id"], duration_minutes=chosen["duration_minutes"])
                     if use_slots else [])
            return render(request, "studio/book.html", auth=None, tenant=tenant, profile=profile,
                          types=active, chosen=chosen, slots=slots, use_slots=use_slots,
                          error=err, status_code=400)

        if not chosen:
            return _re_render("Please choose a session type.")
        if not (name or "").strip():
            return _re_render("Please tell us your name.")

        avail = has_availability(conn, tenant["id"])
        confirm, when_field = False, requested_at
        if avail:
            # availability is on → the visitor must pick a real, still-open slot (re-checked
            # here so a slot taken since page-load can't be double-booked)
            if not slot.strip() or not is_slot_open(
                    conn, tenant["id"], duration_minutes=chosen["duration_minutes"], slot=slot):
                return _re_render("That time is no longer available — please pick another slot.")
            confirm, when_field = True, slot

        result = request_booking(conn, settings, tenant=tenant, booking_type=chosen, name=name,
                                 email=email, requested_at=when_field, message=message, confirm=confirm)
        when = (when_field or "").replace("T", " ").strip() or "no specific time given"
        deposit_note = ("\nA deposit is due to secure it — a deposit invoice was created and they "
                        "were sent to pay it.\n" if result["invoice"] else "")
        lead_line = ("\nIt's confirmed on your calendar — they're in your CRM as a new lead.\n"
                     if confirm else
                     "\nThey're in your CRM as a new lead with a proposed session — open it in "
                     "your schedule to confirm the time.\n")
        inbox = owner_digest_recipient(conn, tenant["id"])
        if inbox:
            notify(conn, settings, to=inbox, tenant_id=tenant["id"], signed=False,
                   subject=(f"New booking: {chosen['title']}" if confirm
                            else f"New booking request: {chosen['title']}"),
                   body=(f"{name.strip() or email or 'Someone'} booked a session via your booking "
                         f"page.\n\nSession: {chosen['title']}\nTime: {when}\nEmail: {email or '—'}\n"
                         + (f"\nMessage:\n{message.strip()}\n" if (message or '').strip() else "")
                         + deposit_note + lead_line))
        conn.commit()
        invoice = result["invoice"]
    # A deposit is due → send the visitor straight to pay it; otherwise a simple thanks.
    if invoice:
        return RedirectResponse(f"/pay/{invoice['token']}", status_code=303)
    return render(request, "studio/book_thanks.html", auth=None, tenant=tenant,
                  booking_title=chosen["title"], confirmed=confirm)

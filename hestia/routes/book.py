"""Public booking routes — the client-facing self-scheduling page."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..availability import available_slots, has_availability, is_slot_open
from ..ratelimit import enforce
from ..scheduler import (
    appointment_ics,
    book_appointment,
    cancel_by_token,
    get_appointment_by_token,
    get_tenant_by_calendar_token,
    reschedule_by_token,
    schedule_ics,
)
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of

router = APIRouter()


@router.get("/calendar/{token}.ics")
def studio_calendar_feed(request: Request, token: str):
    """Public, token-authorized .ics feed of a studio's sessions — the URL a calendar
    app subscribes to (no login, since the app can't carry a session). Unknown token →
    404; a valid one returns a live, always-parseable calendar (possibly empty)."""
    with db_conn(request) as conn:
        tenant = get_tenant_by_calendar_token(conn, token)
        if not tenant:
            return Response(status_code=404)
        ics = schedule_ics(conn, tenant["id"], days=365)
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": 'inline; filename="studio.ics"'})


@router.get("/book/{token}")
def book_page(request: Request, token: str):
    with db_conn(request) as conn:
        appt = get_appointment_by_token(conn, token)
        if not appt or appt["status"] == "canceled":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, appt["tenant_id"])
        # a confirmed session can be self-rescheduled only if the studio offers open hours
        can_reschedule = appt["status"] == "confirmed" and has_availability(conn, appt["tenant_id"])
    return render(request, "scheduler/book.html", auth=None, appt=appt, tenant=tenant,
                  can_reschedule=can_reschedule)


@router.get("/book/{token}/reschedule")
def book_reschedule_page(request: Request, token: str):
    with db_conn(request) as conn:
        appt = get_appointment_by_token(conn, token)
        if not appt or appt["status"] not in ("proposed", "confirmed"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, appt["tenant_id"])
        if not has_availability(conn, appt["tenant_id"]):     # nothing to self-serve → back
            return RedirectResponse(f"/book/{token}", status_code=303)
        slots = available_slots(conn, appt["tenant_id"], duration_minutes=appt["duration_minutes"])
    return render(request, "scheduler/reschedule.html", auth=None, appt=appt, tenant=tenant, slots=slots)


@router.post("/book/{token}/reschedule")
def book_reschedule_submit(request: Request, token: str, slot: str = Form("")):
    enforce(request, "checkout")
    settings = settings_of(request)
    with db_conn(request) as conn:
        conn.execute("BEGIN IMMEDIATE")                       # serialize: guard the new slot
        appt = get_appointment_by_token(conn, token)
        if not appt or appt["status"] not in ("proposed", "confirmed"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        normalized_slot = slot.replace("T", " ").strip()
        current_slot = (appt.get("starts_at") or "").replace("T", " ").strip()
        if appt["status"] == "confirmed" and normalized_slot and normalized_slot == current_slot:
            return RedirectResponse(f"/book/{token}", status_code=303)
        dur = appt["duration_minutes"]
        if not (slot.strip() and is_slot_open(conn, appt["tenant_id"], duration_minutes=dur, slot=slot)):
            tenant = get_tenant(conn, appt["tenant_id"])
            slots = available_slots(conn, appt["tenant_id"], duration_minutes=dur)
            return render(request, "scheduler/reschedule.html", auth=None, appt=appt, tenant=tenant,
                          slots=slots, error="That time is no longer available — please pick another.",
                          status_code=400)
        if not reschedule_by_token(conn, settings, token=token, new_slot=slot):
            tenant = get_tenant(conn, appt["tenant_id"])
            slots = available_slots(conn, appt["tenant_id"], duration_minutes=dur)
            return render(request, "scheduler/reschedule.html", auth=None, appt=appt, tenant=tenant,
                          slots=slots, error="That booking changed — refresh and try again.",
                          status_code=409)
    return RedirectResponse(f"/book/{token}", status_code=303)


@router.get("/book/{token}/calendar.ics")
def book_calendar(request: Request, token: str):
    """Download the confirmed session as an .ics — 'Add to calendar' for the client."""
    with db_conn(request) as conn:
        appt = get_appointment_by_token(conn, token)
        ics = appointment_ics(conn, appt) if appt else None
    if not ics:
        return render(request, "offer_missing.html", auth=None, status_code=404)
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="session.ics"'})


@router.post("/book/{token}/cancel")
def book_cancel(request: Request, token: str):
    """Client cancels their own booking from the link. Shows a confirmation page;
    a direct re-visit of the (now canceled) booking link still 404s as before."""
    enforce(request, "checkout")
    settings = settings_of(request)
    with db_conn(request) as conn:
        appt = get_appointment_by_token(conn, token)
        if not appt or appt["status"] == "canceled":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, appt["tenant_id"])
        if not cancel_by_token(conn, settings, token):
            return render(request, "offer_missing.html", auth=None, status_code=404)
    return render(request, "scheduler/booking_canceled.html", auth=None, appt=appt, tenant=tenant)


@router.post("/book/{token}")
def book_submit(request: Request, token: str, option_id: str = Form("")):
    enforce(request, "checkout")
    with db_conn(request) as conn:
        appt = get_appointment_by_token(conn, token)
        if not appt or appt["status"] == "canceled":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        # Already booked (or a double submit) → idempotent: show the confirmed page.
        if appt["status"] == "confirmed":
            return RedirectResponse(f"/book/{token}", status_code=303)
        if not option_id.strip().isdigit():
            tenant = get_tenant(conn, appt["tenant_id"])
            return render(request, "scheduler/book.html", auth=None, appt=appt, tenant=tenant,
                          error="Please choose a time.", status_code=400)
        book_appointment(conn, token=token, option_id=int(option_id))
    return RedirectResponse(f"/book/{token}", status_code=303)

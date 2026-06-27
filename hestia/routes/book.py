"""Public booking routes — the client-facing self-scheduling page."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..ratelimit import enforce
from ..scheduler import (
    appointment_ics,
    book_appointment,
    cancel_by_token,
    get_appointment_by_token,
)
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of

router = APIRouter()


@router.get("/book/{token}")
def book_page(request: Request, token: str):
    with db_conn(request) as conn:
        appt = get_appointment_by_token(conn, token)
        if not appt or appt["status"] == "canceled":
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, appt["tenant_id"])
    return render(request, "scheduler/book.html", auth=None, appt=appt, tenant=tenant)


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

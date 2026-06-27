"""Scheduler routes (studio side) — propose sessions, track bookings."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..auth import context_from_session
from ..crm import list_clients, list_projects
from ..scheduler import (
    APPOINTMENT_KINDS,
    KIND_LABELS,
    agenda,
    appointment_ics,
    appointment_public_url,
    cancel_appointment,
    complete_appointment,
    confirm_appointment,
    create_appointment,
    get_appointment,
    list_appointments,
    mark_no_show,
    schedule_ics,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/schedule")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.get("")
def schedule_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        appointments = list_appointments(conn, auth.tenant["id"])
        upcoming = agenda(conn, auth.tenant["id"])
    return render(request, "scheduler/schedule.html", auth=auth, appointments=appointments,
                  agenda=upcoming)


@router.get("/new")
def appointment_new(request: Request, project_id: int | None = None, client_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "scheduler/appointment_new.html", auth=auth, clients=clients,
                  projects=projects, kinds=APPOINTMENT_KINDS, kind_labels=KIND_LABELS,
                  preselect_project=project_id, preselect_client=client_id)


@router.post("")
def appointment_create(request: Request, title: str = Form(...), kind: str = Form("consultation"),
                       options: str = Form(""), location: str = Form(""),
                       duration_minutes: str = Form("60"), notes: str = Form(""),
                       client_id: str = Form(""), project_id: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        option_list = [line for line in options.splitlines() if line.strip()]
        try:
            duration = int(duration_minutes)
        except (ValueError, TypeError):
            duration = 60
        appt = create_appointment(
            conn, tenant_id=auth.tenant["id"], title=title, kind=kind, options=option_list,
            location=location, duration_minutes=duration, notes=notes,
            client_id=int(client_id) if client_id.strip().isdigit() else None,
            project_id=int(project_id) if project_id.strip().isdigit() else None,
        )
    return RedirectResponse(f"/schedule/{appt['id']}", status_code=303)


@router.get("/calendar.ics")           # before /{appt_id} so the literal path wins
def schedule_calendar(request: Request):
    """Subscribe-able .ics feed of the studio's confirmed sessions."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        ics = schedule_ics(conn, auth.tenant["id"])
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="schedule.ics"'})


@router.get("/{appt_id}")
def appointment_detail(request: Request, appt_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        appt = get_appointment(conn, auth.tenant["id"], appt_id)
        if not appt:
            return RedirectResponse("/schedule", status_code=303)
    book_url = appointment_public_url(settings_of(request), appt["token"])
    return render(request, "scheduler/appointment_detail.html", auth=auth, appt=appt,
                  book_url=book_url)


@router.get("/{appt_id}/calendar.ics")
def appointment_calendar(request: Request, appt_id: int):
    """Download the confirmed session as an .ics for the owner's own calendar."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        appt = get_appointment(conn, auth.tenant["id"], appt_id)
        ics = appointment_ics(conn, appt) if appt else None
    if not ics:
        return RedirectResponse(f"/schedule/{appt_id}", status_code=303)
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="session.ics"'})


@router.post("/{appt_id}/confirm")
def appointment_confirm(request: Request, appt_id: int, starts_at: str = Form(...)):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        confirm_appointment(conn, auth.tenant["id"], appt_id, starts_at)
    return RedirectResponse(f"/schedule/{appt_id}", status_code=303)


@router.post("/{appt_id}/cancel")
def appointment_cancel(request: Request, appt_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        cancel_appointment(conn, auth.tenant["id"], appt_id)
    return RedirectResponse(f"/schedule/{appt_id}", status_code=303)


@router.post("/{appt_id}/complete")
def appointment_complete(request: Request, appt_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        complete_appointment(conn, auth.tenant["id"], appt_id)
    return RedirectResponse(f"/schedule/{appt_id}", status_code=303)


@router.post("/{appt_id}/no-show")
def appointment_no_show(request: Request, appt_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        mark_no_show(conn, auth.tenant["id"], appt_id)
    return RedirectResponse(f"/schedule/{appt_id}", status_code=303)

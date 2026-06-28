"""Recurring-invoice routes (studio side) — retainer/subscription billing on a cadence.

The profiles here are templates; the worker sweep (:func:`hestia.recurring.run_recurring`)
spawns the actual invoices, which the client pays at the usual ``/pay/{token}`` link.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..crm import get_client, get_project, list_clients, list_projects
from ..invoices import money
from ..recurring import (
    CADENCES,
    create_recurring,
    delete_recurring,
    list_recurring,
    set_recurring_active,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/recurring")


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


@router.get("")
def recurring_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        profiles = list_recurring(conn, auth.tenant["id"])
        currency = settings_of(request).currency
        for p in profiles:
            p["amount_display"] = money(p["amount_cents"], currency)
    return render(request, "recurring/recurring.html", auth=auth, profiles=profiles)


@router.get("/new")
def recurring_new(request: Request, client_id: int | None = None, project_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "recurring/recurring_new.html", auth=auth, clients=clients,
                  projects=projects, preselect_client=client_id, preselect_project=project_id,
                  cadences=list(CADENCES))


@router.post("")
def recurring_create(request: Request, title: str = Form(...), amount: str = Form("0"),
                     cadence: str = Form("monthly"), next_run_at: str = Form(""),
                     client_id: str = Form(""), project_id: str = Form(""), note: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        # only attach a client/project this studio actually owns — a stray cross-tenant id
        # would otherwise ride along on the profile and every invoice it spawns
        raw_c = int(client_id) if client_id.strip().isdigit() else None
        cid = raw_c if raw_c and get_client(conn, tid, raw_c) else None
        raw_p = int(project_id) if project_id.strip().isdigit() else None
        pid = raw_p if raw_p and get_project(conn, tid, raw_p) else None
        create_recurring(
            conn, tenant_id=tid, title=title, amount_cents=_to_cents(amount),
            cadence=cadence, next_run_at=next_run_at, client_id=cid, project_id=pid, note=note,
        )
    return RedirectResponse("/recurring", status_code=303)


@router.post("/{recurring_id}/pause")
def recurring_pause(request: Request, recurring_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_recurring_active(conn, auth.tenant["id"], recurring_id, False)
    return RedirectResponse("/recurring", status_code=303)


@router.post("/{recurring_id}/resume")
def recurring_resume(request: Request, recurring_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_recurring_active(conn, auth.tenant["id"], recurring_id, True)
    return RedirectResponse("/recurring", status_code=303)


@router.post("/{recurring_id}/delete")
def recurring_delete(request: Request, recurring_id: int):
    """Remove a recurring profile entirely. Already-generated invoices are unaffected."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_recurring(conn, auth.tenant["id"], recurring_id)
    return RedirectResponse("/recurring", status_code=303)

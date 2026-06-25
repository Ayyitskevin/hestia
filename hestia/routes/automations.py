"""Automation routes (studio side) — define rules and watch them run."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..automations import (
    ACTIONS,
    TRIGGERS,
    create_automation,
    delete_automation,
    get_automation,
    list_automations,
    list_runs,
    set_automation_enabled,
)
from ..db import audit
from .deps import db_conn, render

router = APIRouter(prefix="/automations")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.get("")
def automations_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        automations = list_automations(conn, auth.tenant["id"])
        runs = list_runs(conn, auth.tenant["id"], limit=25)
    return render(request, "automations/automations.html", auth=auth,
                  automations=automations, runs=runs)


@router.get("/new")
def automation_new(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
    return render(request, "automations/automation_new.html", auth=auth,
                  triggers=TRIGGERS, actions=ACTIONS)


@router.post("")
def automation_create(request: Request, name: str = Form(...), trigger: str = Form(...),
                      subject: str = Form(""), body: str = Form(""),
                      action: str = Form("email_client")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        auto = create_automation(conn, tenant_id=auth.tenant["id"], name=name, trigger=trigger,
                                 subject=subject, body=body, action=action)
        if auto:
            audit(conn, actor="owner", action="automation.created", tenant_id=auth.tenant["id"],
                  detail=f"{auto['name']} · on {trigger}")
    return RedirectResponse("/automations", status_code=303)


@router.post("/{automation_id}/toggle")
def automation_toggle(request: Request, automation_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        auto = get_automation(conn, auth.tenant["id"], automation_id)
        if auto:
            set_automation_enabled(conn, auth.tenant["id"], automation_id,
                                   not bool(auto["enabled"]))
    return RedirectResponse("/automations", status_code=303)


@router.post("/{automation_id}/delete")
def automation_delete(request: Request, automation_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_automation(conn, auth.tenant["id"], automation_id)
    return RedirectResponse("/automations", status_code=303)

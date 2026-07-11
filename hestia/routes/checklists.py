"""Checklist-template routes (studio side) — the reusable per-shoot-type deliverable lists.

The templates here are copied onto a project's checklist when it books (automatically) or
from the project page on demand; see :mod:`hestia.checklists`.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..checklists import ANY, add_template_task, delete_template_task, list_template_tasks
from ..features import SHOOT_TYPE_LABELS, SHOOT_TYPES
from .deps import db_conn, render, tenant_user

router = APIRouter(prefix="/checklists")




@router.get("")
def checklists_page(request: Request):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        templates = list_template_tasks(conn, auth.tenant["id"])
    labels = {**SHOOT_TYPE_LABELS, ANY: "Any shoot type"}
    # group items by shoot type for display, preserving order
    grouped: dict[str, list] = {}
    for t in templates:
        grouped.setdefault(t["shoot_type"], []).append(t)
    return render(request, "crm/checklists.html", auth=auth, grouped=grouped, labels=labels,
                  shoot_types=[ANY, *SHOOT_TYPES])


@router.post("")
def checklist_add(request: Request, shoot_type: str = Form("any"), label: str = Form("")):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        add_template_task(conn, tenant_id=auth.tenant["id"], shoot_type=shoot_type, label=label)
    return RedirectResponse("/checklists", status_code=303)


@router.post("/{template_id}/delete")
def checklist_delete(request: Request, template_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_template_task(conn, auth.tenant["id"], template_id)
    return RedirectResponse("/checklists", status_code=303)

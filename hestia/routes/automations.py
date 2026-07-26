"""Automation routes (studio side) — define rules and watch them run."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..automations import (
    ACTIONS,
    RETENTION_RECIPES,
    TRIGGERS,
    create_automation,
    create_from_recipe,
    delete_automation,
    get_automation,
    list_automations,
    list_runs,
    set_automation_enabled,
)
from ..db import audit
from .deps import db_conn, render, settings_of, tenant_user

router = APIRouter(prefix="/automations")
_SQLITE_INT64_MAX = (1 << 63) - 1


def _gallery_return_path(conn, tenant_id: str, raw_gallery_id: str) -> str | None:
    value = (raw_gallery_id or "").strip()
    if not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        return None
    gallery_id = int(value)
    if not 1 <= gallery_id <= _SQLITE_INT64_MAX:
        return None
    gallery = conn.execute(
        "SELECT status FROM galleries WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    if not gallery:
        return None
    fragment = "proofing-preflight" if gallery["status"] == "draft" else "client-proofing"
    return f"/galleries/{gallery_id}#{fragment}"


@router.get("")
def automations_list(request: Request):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        automations = list_automations(conn, auth.tenant["id"])
        runs = list_runs(conn, auth.tenant["id"], limit=25)
    return render(
        request,
        "automations/automations.html",
        auth=auth,
        automations=automations,
        runs=runs,
        recipes=RETENTION_RECIPES,
        automation_email_enabled=settings_of(request).automation_email_enabled,
    )


@router.get("/new")
def automation_new(
    request: Request,
    trigger: str = "",
    gallery_id: str = "",
    error: str = "",
):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        return_path = _gallery_return_path(conn, auth.tenant["id"], gallery_id)
    selected_trigger = trigger if trigger in TRIGGERS else next(iter(TRIGGERS))
    return render(
        request,
        "automations/automation_new.html",
        auth=auth,
        triggers=TRIGGERS,
        actions=ACTIONS,
        selected_trigger=selected_trigger,
        gallery_id=gallery_id if return_path else "",
        gallery_prefill=selected_trigger == "gallery.published",
        return_path=return_path,
        automation_error=error == "invalid",
        automation_email_enabled=settings_of(request).automation_email_enabled,
    )


@router.post("")
def automation_create(
    request: Request,
    name: str = Form(...),
    trigger: str = Form(...),
    subject: str = Form(""),
    body: str = Form(""),
    action: str = Form("email_client"),
    delay_days: str = Form("0"),
    gallery_id: str = Form(""),
):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        return_path = _gallery_return_path(conn, auth.tenant["id"], gallery_id)
        try:
            delay = int(delay_days)
        except (ValueError, TypeError):
            delay = 0
        auto = create_automation(conn, tenant_id=auth.tenant["id"], name=name, trigger=trigger,
                                 subject=subject, body=body, action=action, delay_days=delay)
        if auto:
            audit(conn, actor="owner", action="automation.created", tenant_id=auth.tenant["id"],
                  detail=f"{auto['name']} · on {trigger}")
    if not auto:
        target = "/automations/new?error=invalid"
        if return_path:
            target += f"&trigger=gallery.published&gallery_id={gallery_id}"
        return RedirectResponse(target, status_code=303)
    return RedirectResponse(return_path or "/automations", status_code=303)


@router.post("/recipe")
def automation_recipe(request: Request, key: str = Form(...)):
    """One-click create a retention automation from a preset recipe."""
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        auto = create_from_recipe(conn, auth.tenant["id"], key)
        if auto:
            audit(conn, actor="owner", action="automation.created", tenant_id=auth.tenant["id"],
                  detail=f"{auto['name']} (recipe)")
    return RedirectResponse("/automations", status_code=303)


@router.post("/{automation_id}/toggle")
def automation_toggle(request: Request, automation_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_automation(conn, auth.tenant["id"], automation_id)
    return RedirectResponse("/automations", status_code=303)

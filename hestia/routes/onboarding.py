"""Owner onboarding presets."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..presets import PRESETS, apply_preset, preset_applied
from .deps import db_conn, render

router = APIRouter()


@router.get("/onboarding")
def onboarding_form(request: Request):
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or auth.is_admin or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        applied = preset_applied(conn, auth.tenant["id"])
    return render(request, "onboarding.html", auth=auth, presets=PRESETS, applied=applied)


@router.post("/onboarding")
def onboarding_apply(
    request: Request,
    preset: str = Form(...),
    include_demo: str = Form(""),
):
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or auth.is_admin or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        apply_preset(
            conn,
            auth.tenant["id"],
            preset,
            include_demo=bool(include_demo),
            actor="owner",
        )
    return RedirectResponse("/dashboard", status_code=303)

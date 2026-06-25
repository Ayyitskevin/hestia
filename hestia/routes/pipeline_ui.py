"""Pipeline stepper UI: watch vision → offer go green, then click the offer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..pipeline import load_run, run_public_dict
from .deps import db_conn, render

router = APIRouter()


@router.get("/pipeline/{run_id}")
def stepper_page(request: Request, run_id: int):
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        run = load_run(conn, run_id)
        if not run or run["tenant_id"] != auth.tenant["id"]:
            return RedirectResponse("/galleries", status_code=303)
    return render(request, "pipeline.html", auth=auth, run=run_public_dict(run))


@router.get("/pipeline/{run_id}/partial")
def stepper_partial(request: Request, run_id: int):
    with db_conn(request) as conn:
        auth = context_from_session(conn, request)
        if not auth or not auth.tenant:
            return RedirectResponse("/login", status_code=303)
        run = load_run(conn, run_id)
        if not run or run["tenant_id"] != auth.tenant["id"]:
            return RedirectResponse("/galleries", status_code=303)
    return render(request, "_stepper.html", auth=auth, run=run_public_dict(run))

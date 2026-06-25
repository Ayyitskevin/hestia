"""JSON API: process a gallery and read run status.

Auth accepts a session cookie (logged-in owner) or an
``Authorization: Bearer hestia_tk_<slug>_<secret>`` API key (automation).
"""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel

from ..galleries import get_gallery
from ..jobs import drain, enqueue
from ..pipeline import list_runs, load_run, run_public_dict, start_run
from ..tenants import get_tenant
from .deps import auth_context, db_conn, settings_of

router = APIRouter(prefix="/api")


class ProcessRequest(BaseModel):
    gallery_id: int


def _require_tenant(request: Request) -> dict:
    ctx = auth_context(request)
    if not ctx or not ctx.tenant:
        raise HTTPException(status_code=401, detail="tenant authentication required")
    return ctx.tenant


def _schedule(request: Request, background_tasks: BackgroundTasks) -> None:
    settings = settings_of(request)
    background_tasks.add_task(drain, settings.db_path, settings)


@router.post("/pipeline/run")
def pipeline_run(request: Request, body: ProcessRequest, background_tasks: BackgroundTasks):
    tenant = _require_tenant(request)
    with db_conn(request) as conn:
        gallery = get_gallery(conn, tenant["id"], body.gallery_id)
        if not gallery:
            raise HTTPException(status_code=404, detail="gallery not found")
        full_tenant = get_tenant(conn, tenant["id"])
        run = start_run(conn, tenant=full_tenant, gallery_id=body.gallery_id)
        enqueue(conn, kind="pipeline.run", payload={"run_id": run["id"]}, tenant_id=tenant["id"])
    _schedule(request, background_tasks)
    return run_public_dict(run)


@router.get("/pipeline/runs")
def pipeline_runs(request: Request):
    tenant = _require_tenant(request)
    with db_conn(request) as conn:
        runs = list_runs(conn, tenant["id"], limit=50)
    return {"runs": [run_public_dict(r) for r in runs]}


@router.get("/pipeline/runs/{run_id}")
def pipeline_run_status(request: Request, run_id: int):
    tenant = _require_tenant(request)
    with db_conn(request) as conn:
        run = load_run(conn, run_id)
    if not run or run["tenant_id"] != tenant["id"]:
        raise HTTPException(status_code=404, detail="run not found")
    return run_public_dict(run)

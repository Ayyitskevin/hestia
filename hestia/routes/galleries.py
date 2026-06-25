"""Gallery routes — the native product surface: create, upload, process, offer."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse

from ..albums import get_album_for_gallery
from ..auth import context_from_session
from ..crm import assign_gallery_to_project, get_project, list_projects
from ..db import audit
from ..galleries import (
    add_image,
    create_gallery,
    get_gallery,
    list_galleries,
    list_images,
    publish_gallery,
)
from ..jobs import drain, enqueue
from ..pipeline import start_run
from ..products import get_set_for_gallery
from ..sales import get_offer_for_gallery, offer_public_url
from ..tenants import get_tenant, tenant_flags
from .deps import db_conn, render, settings_of, storage_of

router = APIRouter(prefix="/galleries")


def _require_user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _schedule(request: Request, background_tasks: BackgroundTasks) -> None:
    """Kick an inline drain so work starts now; the worker thread is the backstop."""
    settings = settings_of(request)
    background_tasks.add_task(drain, settings.db_path, settings)


@router.get("")
def gallery_list(request: Request):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        galleries = list_galleries(conn, auth.tenant["id"])
    return render(request, "galleries.html", auth=auth, galleries=galleries)


@router.get("/new")
def gallery_new(request: Request, project_id: int | None = None):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "gallery_new.html", auth=auth, projects=projects,
                  preselect_project=project_id)


@router.post("")
def gallery_create(
    request: Request,
    title: str = Form(...),
    client_name: str = Form(""),
    pin: str = Form(""),
    project_id: str = Form(""),
):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = create_gallery(conn, tenant_id=auth.tenant["id"], title=title,
                                 client_name=client_name, pin=pin.strip() or None)
        if project_id.strip().isdigit():
            assign_gallery_to_project(conn, auth.tenant["id"], gallery["id"], int(project_id))
    return RedirectResponse(f"/galleries/{gallery['id']}", status_code=303)


@router.get("/{gallery_id}")
def gallery_detail(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        images = list_images(conn, gallery_id)
        offer = get_offer_for_gallery(conn, auth.tenant["id"], gallery_id)
        run = conn.execute(
            "SELECT id, status FROM pipeline_runs WHERE tenant_id = ? AND source='gallery' AND source_id = ?",
            (auth.tenant["id"], str(gallery_id)),
        ).fetchone()
        flags = tenant_flags(get_tenant(conn, auth.tenant["id"]))
        project = get_project(conn, auth.tenant["id"], gallery["project_id"]) if gallery.get("project_id") else None
        album = get_album_for_gallery(conn, auth.tenant["id"], gallery_id)
        product_set = get_set_for_gallery(conn, auth.tenant["id"], gallery_id)
    offer_url = offer_public_url(settings_of(request), auth.tenant["slug"], offer["token"]) if offer else None
    return render(request, "gallery_detail.html", auth=auth, gallery=gallery, images=images,
                  offer=offer, offer_url=offer_url, run=dict(run) if run else None,
                  storage=storage_of(request), flags=flags, project=project, album=album,
                  product_set=product_set)


@router.post("/{gallery_id}/images")
async def gallery_upload(request: Request, gallery_id: int, files: list[UploadFile] = File(...)):
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        for up in files:
            if not up.filename:
                continue
            add_image(conn, storage, tenant_id=auth.tenant["id"], gallery_id=gallery_id,
                      filename=up.filename, fileobj=up.file,
                      content_type=up.content_type or "application/octet-stream")
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/publish")
def gallery_publish(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        publish_gallery(conn, auth.tenant["id"], gallery_id)
        audit(conn, actor="owner", action="gallery.published",
              tenant_id=auth.tenant["id"], detail=gallery["title"])
    return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)


@router.post("/{gallery_id}/process")
def gallery_process(request: Request, gallery_id: int, background_tasks: BackgroundTasks):
    with db_conn(request) as conn:
        auth = _require_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        run = start_run(conn, tenant=tenant, gallery_id=gallery_id)
        enqueue(conn, kind="pipeline.run", payload={"run_id": run["id"]}, tenant_id=tenant["id"])
    _schedule(request, background_tasks)
    return RedirectResponse(f"/pipeline/{run['id']}", status_code=303)

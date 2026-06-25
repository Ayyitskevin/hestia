"""Marketing content routes — generate and view content packs for a project."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..content import approve_pack, generate_pack, get_pack
from ..crm import get_project
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.post("/projects/{project_id}/content")
def content_generate(request: Request, project_id: int, recipe: str = Form("social-set")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        project = get_project(conn, auth.tenant["id"], project_id)
        if not project:
            return RedirectResponse("/projects", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        pack = generate_pack(conn, settings_of(request), tenant=tenant, project=project, recipe=recipe)
    return RedirectResponse(f"/content/{pack['id']}", status_code=303)


@router.get("/content/{pack_id}")
def content_view(request: Request, pack_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        pack = get_pack(conn, auth.tenant["id"], pack_id)
        if not pack:
            return RedirectResponse("/projects", status_code=303)
    return render(request, "content/pack.html", auth=auth, pack=pack)


@router.post("/content/{pack_id}/approve")
def content_approve(request: Request, pack_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        approve_pack(conn, auth.tenant["id"], pack_id)
    return RedirectResponse(f"/content/{pack_id}", status_code=303)

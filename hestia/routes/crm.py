"""CRM routes — clients and projects (studio-OS backbone)."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..content import list_packs, recipes_for
from ..contracts import list_contracts
from ..crm import (
    PROJECT_STATUSES,
    create_client,
    create_project,
    galleries_for_project,
    get_client,
    get_project,
    list_clients,
    list_projects,
    set_project_status,
)
from ..invoices import list_invoices
from .deps import db_conn, render

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


# ── Clients ─────────────────────────────────────────────────────────────────


@router.get("/clients")
def clients_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
    return render(request, "crm/clients.html", auth=auth, clients=clients)


@router.get("/clients/new")
def client_new(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
    return render(request, "crm/client_new.html", auth=auth)


@router.post("/clients")
def client_create(request: Request, name: str = Form(...), email: str = Form(""),
                  phone: str = Form(""), notes: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        client = create_client(conn, tenant_id=auth.tenant["id"], name=name,
                               email=email, phone=phone, notes=notes)
    return RedirectResponse(f"/clients/{client['id']}", status_code=303)


@router.get("/clients/{client_id}")
def client_detail(request: Request, client_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        client = get_client(conn, auth.tenant["id"], client_id)
        if not client:
            return RedirectResponse("/clients", status_code=303)
        projects = list_projects(conn, auth.tenant["id"], client_id=client_id)
    return render(request, "crm/client_detail.html", auth=auth, client=client, projects=projects)


# ── Projects ────────────────────────────────────────────────────────────────


@router.get("/projects")
def projects_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "crm/projects.html", auth=auth, projects=projects,
                  statuses=PROJECT_STATUSES)


@router.get("/projects/new")
def project_new(request: Request, client_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
    return render(request, "crm/project_new.html", auth=auth, clients=clients,
                  preselect_client=client_id, statuses=PROJECT_STATUSES)


@router.post("/projects")
def project_create(request: Request, name: str = Form(...), client_id: str = Form(""),
                   shoot_type: str = Form("other"), status: str = Form("lead"),
                   event_date: str = Form(""), notes: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        cid = int(client_id) if client_id.strip().isdigit() else None
        project = create_project(conn, tenant_id=auth.tenant["id"], name=name, client_id=cid,
                                 shoot_type=shoot_type, status=status, event_date=event_date,
                                 notes=notes)
    return RedirectResponse(f"/projects/{project['id']}", status_code=303)


@router.get("/projects/{project_id}")
def project_detail(request: Request, project_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        project = get_project(conn, auth.tenant["id"], project_id)
        if not project:
            return RedirectResponse("/projects", status_code=303)
        galleries = galleries_for_project(conn, auth.tenant["id"], project_id)
        invoices = list_invoices(conn, auth.tenant["id"], project_id=project_id)
        contracts = list_contracts(conn, auth.tenant["id"], project_id=project_id)
        packs = list_packs(conn, auth.tenant["id"], project_id=project_id)
        recipes = recipes_for(project["shoot_type"])
    return render(request, "crm/project_detail.html", auth=auth, project=project,
                  galleries=galleries, invoices=invoices, contracts=contracts, packs=packs,
                  recipes=recipes, statuses=PROJECT_STATUSES)


@router.post("/projects/{project_id}/status")
def project_status(request: Request, project_id: int, status: str = Form(...)):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_project_status(conn, auth.tenant["id"], project_id, status)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

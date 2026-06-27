"""Contract routes (studio side) — draft, send for signature, and track signing."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import messaging
from ..auth import context_from_session
from ..contracts import (
    contract_public_url,
    create_contract,
    get_contract,
    list_contracts,
    send_contract,
    void_contract,
)
from ..crm import list_clients, list_projects
from ..db import audit
from ..email import notify
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/contracts")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.get("")
def contracts_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        contracts = list_contracts(conn, auth.tenant["id"])
    return render(request, "contracts/contracts.html", auth=auth, contracts=contracts)


@router.get("/new")
def contract_new(request: Request, project_id: int | None = None, client_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "contracts/contract_new.html", auth=auth, clients=clients,
                  projects=projects, preselect_project=project_id, preselect_client=client_id)


@router.post("")
def contract_create(request: Request, title: str = Form(...), body: str = Form(""),
                    signer_name: str = Form(""), signer_email: str = Form(""),
                    client_id: str = Form(""), project_id: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        contract = create_contract(
            conn, tenant_id=auth.tenant["id"], title=title, body=body,
            signer_name=signer_name, signer_email=signer_email,
            client_id=int(client_id) if client_id.strip().isdigit() else None,
            project_id=int(project_id) if project_id.strip().isdigit() else None,
        )
        audit(conn, actor="owner", action="contract.created", tenant_id=auth.tenant["id"],
              detail=contract["title"])
    return RedirectResponse(f"/contracts/{contract['id']}", status_code=303)


@router.get("/{contract_id}")
def contract_detail(request: Request, contract_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        contract = get_contract(conn, auth.tenant["id"], contract_id)
        if not contract:
            return RedirectResponse("/contracts", status_code=303)
    sign_url = contract_public_url(settings_of(request), contract["token"])
    return render(request, "contracts/contract_detail.html", auth=auth,
                  contract=contract, sign_url=sign_url)


@router.post("/{contract_id}/send")
def contract_send(request: Request, contract_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        send_contract(conn, auth.tenant["id"], contract_id)
        contract = get_contract(conn, auth.tenant["id"], contract_id)
        if contract:
            audit(conn, actor="owner", action="contract.sent", tenant_id=auth.tenant["id"],
                  detail=contract["title"])
            # Email the client their sign link (mock records it; smtp also delivers).
            to = contract.get("signer_email") or contract.get("client_email")
            if to:
                ctx = {
                    "client": contract.get("signer_name") or contract.get("client_name") or "there",
                    "studio": auth.tenant.get("name", "your photographer"),
                    "title": contract["title"],
                    "sign_url": contract_public_url(settings, contract["token"]),
                }
                msg = messaging.render(conn, auth.tenant["id"], "contract_send", ctx)
                notify(conn, settings, to=to, tenant_id=auth.tenant["id"],
                       subject=msg["subject"], body=msg["body"])
        conn.commit()
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)


@router.post("/{contract_id}/void")
def contract_void(request: Request, contract_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        contract = get_contract(conn, auth.tenant["id"], contract_id)
        void_contract(conn, auth.tenant["id"], contract_id)
        if contract:
            audit(conn, actor="owner", action="contract.void", tenant_id=auth.tenant["id"],
                  detail=contract["title"])
    return RedirectResponse(f"/contracts/{contract_id}", status_code=303)

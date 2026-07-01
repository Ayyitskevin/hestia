"""Proposal routes — one polished link for quote, agreement, and deposit."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..contracts import contract_public_url
from ..crm import get_client, get_project, list_clients, list_projects
from ..db import audit
from ..email import notify
from ..invoices import invoice_public_url
from ..packages import get_package, list_packages
from ..proposals import (
    accept_proposal,
    create_proposal,
    get_proposal,
    get_proposal_by_token,
    list_proposals,
    proposal_followups,
    proposal_public_url,
    record_proposal_reminder,
    record_proposal_view,
    send_proposal,
    send_proposal_reminder,
    void_proposal,
)
from ..ratelimit import enforce
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/proposals")
public_router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _optional_int(raw: str) -> int | None:
    raw = (raw or "").strip()
    return int(raw) if raw.isdigit() else None


def _link_context(request: Request, proposal: dict) -> dict:
    settings = settings_of(request)
    return {
        "proposal_url": proposal_public_url(settings, proposal["token"]),
        "sign_url": contract_public_url(settings, proposal["contract_token"]),
        "pay_url": invoice_public_url(settings, proposal["invoice_token"]),
    }


@router.get("")
def proposals_list(request: Request, status: str = ""):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        proposals = list_proposals(conn, auth.tenant["id"], status=status or None)
        followups = proposal_followups(conn, auth.tenant["id"], limit=200)
    return render(request, "proposals/proposals.html", auth=auth, proposals=proposals,
                  active_status=status, followups=followups)


@router.get("/new")
def proposal_new(request: Request, package_id: int | None = None, client_id: int | None = None,
                 project_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        clients = list_clients(conn, tid)
        projects = list_projects(conn, tid)
        packages = list_packages(conn, tid, active_only=True)
        prefill = get_package(conn, tid, package_id) if package_id else None
    return render(request, "proposals/proposal_new.html", auth=auth, clients=clients,
                  projects=projects, packages=packages, prefill=prefill,
                  preselect_client=client_id, preselect_project=project_id)


@router.post("")
def proposal_create(request: Request, package_id: str = Form(""), title: str = Form(""),
                    summary: str = Form(""), terms: str = Form(""),
                    signer_name: str = Form(""), signer_email: str = Form(""),
                    client_id: str = Form(""), project_id: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        cid = _optional_int(client_id)
        pid = _optional_int(project_id)
        # Validate for the form response and keep signer defaults friendly; the
        # proposal module still re-normalizes ids before writing.
        client = get_client(conn, tid, cid) if cid else None
        project = get_project(conn, tid, pid) if pid else None
        if client and not signer_name.strip():
            signer_name = client["name"]
        if client and not signer_email.strip():
            signer_email = client.get("email") or ""
        proposal = create_proposal(
            conn,
            settings_of(request),
            tenant_id=tid,
            package_id=_optional_int(package_id) or 0,
            title=title,
            summary=summary,
            terms=terms,
            client_id=client["id"] if client else None,
            project_id=project["id"] if project else None,
            signer_name=signer_name,
            signer_email=signer_email,
        )
    if not proposal:
        return RedirectResponse("/proposals/new", status_code=303)
    return RedirectResponse(f"/proposals/{proposal['id']}", status_code=303)


@router.get("/{proposal_id}")
def proposal_detail(request: Request, proposal_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        proposal = get_proposal(conn, auth.tenant["id"], proposal_id)
        if not proposal:
            return RedirectResponse("/proposals", status_code=303)
    return render(request, "proposals/proposal_detail.html", auth=auth, proposal=proposal,
                  **_link_context(request, proposal))


@router.post("/{proposal_id}/send")
def proposal_send(request: Request, proposal_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        proposal = send_proposal(conn, auth.tenant["id"], proposal_id)
        if proposal and proposal.get("client_email"):
            url = proposal_public_url(settings, proposal["token"])
            notify(
                conn,
                settings,
                to=proposal["client_email"],
                tenant_id=auth.tenant["id"],
                subject=f"{auth.tenant['name']}: {proposal['title']} proposal",
                body=(
                    f"Hi {proposal.get('client_name') or 'there'},\n\n"
                    f"Your {proposal['title']} proposal is ready:\n{url}\n\n"
                    "You can review the package, accept the proposal, sign the agreement, "
                    "and pay the booking invoice from that link."
                ),
            )
    return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)


@router.post("/{proposal_id}/remind")
def proposal_remind(request: Request, proposal_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        tid = auth.tenant["id"]
        if record_proposal_reminder(conn, tid, proposal_id):
            proposal = get_proposal(conn, tid, proposal_id)
            if proposal and send_proposal_reminder(conn, settings, proposal):
                audit(conn, actor="owner", action="proposal.reminded", tenant_id=tid,
                      detail=f"{proposal['title']} · reminder #{proposal['reminder_count']}")
        conn.commit()
    return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)


@router.post("/{proposal_id}/void")
def proposal_void(request: Request, proposal_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        void_proposal(conn, auth.tenant["id"], proposal_id)
    return RedirectResponse(f"/proposals/{proposal_id}", status_code=303)


@public_router.get("/proposal/{token}")
def public_proposal(request: Request, token: str):
    with db_conn(request) as conn:
        proposal = get_proposal_by_token(conn, token)
        if not proposal or proposal["status"] in ("draft", "void"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        record_proposal_view(conn, token)
        tenant = get_tenant(conn, proposal["tenant_id"])
    return render(request, "proposals/proposal_public.html", auth=None, proposal=proposal,
                  tenant=tenant, **_link_context(request, proposal))


@public_router.post("/proposal/{token}/accept")
def public_proposal_accept(request: Request, token: str, accepted_name: str = Form(""),
                           accepted_email: str = Form(""), agree: str = Form("")):
    enforce(request, "checkout")
    with db_conn(request) as conn:
        proposal = get_proposal_by_token(conn, token)
        if not proposal or proposal["status"] in ("draft", "void"):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, proposal["tenant_id"])
        if proposal["status"] == "accepted":
            return RedirectResponse(f"/proposal/{token}", status_code=303)
        if not agree or not accepted_name.strip():
            return render(request, "proposals/proposal_public.html", auth=None,
                          proposal=proposal, tenant=tenant, error="Please enter your name and accept the proposal.",
                          status_code=400, **_link_context(request, proposal))
        accept_proposal(conn, token=token, accepted_name=accepted_name,
                        accepted_email=accepted_email)
    return RedirectResponse(f"/proposal/{token}", status_code=303)

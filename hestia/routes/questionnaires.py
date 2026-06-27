"""Questionnaire routes (studio side) — draft intake forms, send, track answers."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import messaging
from ..auth import context_from_session
from ..crm import list_clients, list_projects
from ..db import audit
from ..email import notify
from ..questionnaires import (
    create_questionnaire,
    delete_questionnaire_template,
    get_questionnaire,
    get_questionnaire_template,
    list_questionnaire_templates,
    list_questionnaires,
    questionnaire_public_url,
    save_questionnaire_template,
    send_questionnaire,
    void_questionnaire,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/questionnaires")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.get("")
def questionnaires_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        questionnaires = list_questionnaires(conn, auth.tenant["id"])
    return render(request, "questionnaires/questionnaires.html", auth=auth,
                  questionnaires=questionnaires)


@router.get("/new")
def questionnaire_new(request: Request, project_id: int | None = None,
                      client_id: int | None = None, template_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
        projects = list_projects(conn, auth.tenant["id"])
        templates = list_questionnaire_templates(conn, auth.tenant["id"])
        prefill_prompts = ""
        if template_id:                         # "start from template" pre-fills the questions
            tpl = get_questionnaire_template(conn, auth.tenant["id"], template_id)
            prefill_prompts = tpl["prompts"] if tpl else ""
    return render(request, "questionnaires/questionnaire_new.html", auth=auth, clients=clients,
                  projects=projects, preselect_project=project_id, preselect_client=client_id,
                  templates=templates, prefill_prompts=prefill_prompts)


# ── reusable question-set templates (must precede /{qid} so "templates" wins) ──


@router.get("/templates")
def questionnaire_templates_page(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        templates = list_questionnaire_templates(conn, auth.tenant["id"])
    return render(request, "questionnaires/questionnaire_templates.html", auth=auth,
                  templates=templates)


@router.post("/templates")
def questionnaire_template_create(request: Request, name: str = Form(""), prompts: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        save_questionnaire_template(conn, tenant_id=auth.tenant["id"], name=name, prompts=prompts)
    return RedirectResponse("/questionnaires/templates", status_code=303)


@router.post("/templates/{template_id}/delete")
def questionnaire_template_delete(request: Request, template_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_questionnaire_template(conn, auth.tenant["id"], template_id)
    return RedirectResponse("/questionnaires/templates", status_code=303)


@router.post("")
def questionnaire_create(request: Request, title: str = Form(...), prompts: str = Form(""),
                         client_id: str = Form(""), project_id: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        prompt_list = [line for line in prompts.splitlines() if line.strip()]
        q = create_questionnaire(
            conn, tenant_id=auth.tenant["id"], title=title, prompts=prompt_list,
            client_id=int(client_id) if client_id.strip().isdigit() else None,
            project_id=int(project_id) if project_id.strip().isdigit() else None,
        )
        audit(conn, actor="owner", action="questionnaire.created", tenant_id=auth.tenant["id"],
              detail=q["title"])
    return RedirectResponse(f"/questionnaires/{q['id']}", status_code=303)


@router.get("/{qid}")
def questionnaire_detail(request: Request, qid: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        q = get_questionnaire(conn, auth.tenant["id"], qid)
        if not q:
            return RedirectResponse("/questionnaires", status_code=303)
    fill_url = questionnaire_public_url(settings_of(request), q["token"])
    return render(request, "questionnaires/questionnaire_detail.html", auth=auth, q=q,
                  fill_url=fill_url)


@router.post("/{qid}/send")
def questionnaire_send(request: Request, qid: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        send_questionnaire(conn, auth.tenant["id"], qid)
        q = get_questionnaire(conn, auth.tenant["id"], qid)
        if q:
            audit(conn, actor="owner", action="questionnaire.sent", tenant_id=auth.tenant["id"],
                  detail=q["title"])
            to = q.get("client_email")
            if to:
                ctx = {
                    "client": q.get("client_name") or "there",
                    "studio": auth.tenant.get("name", "your photographer"),
                    "title": q["title"],
                    "fill_url": questionnaire_public_url(settings, q["token"]),
                }
                msg = messaging.render(conn, auth.tenant["id"], "questionnaire_send", ctx)
                notify(conn, settings, to=to, tenant_id=auth.tenant["id"],
                       subject=msg["subject"], body=msg["body"])
        conn.commit()
    return RedirectResponse(f"/questionnaires/{qid}", status_code=303)


@router.post("/{qid}/void")
def questionnaire_void(request: Request, qid: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        q = get_questionnaire(conn, auth.tenant["id"], qid)
        void_questionnaire(conn, auth.tenant["id"], qid)
        if q:
            audit(conn, actor="owner", action="questionnaire.void", tenant_id=auth.tenant["id"],
                  detail=q["title"])
    return RedirectResponse(f"/questionnaires/{qid}", status_code=303)

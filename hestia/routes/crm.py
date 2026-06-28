"""CRM routes — clients and projects (studio-OS backbone)."""

from __future__ import annotations

import csv
import io
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from .. import messaging
from ..auth import context_from_session
from ..content import list_packs, recipes_for
from ..contracts import list_contracts
from ..crm import (
    PROJECT_STATUSES,
    add_client_tag,
    all_tags,
    client_timeline,
    create_client,
    create_project,
    galleries_for_project,
    get_client,
    get_project,
    list_clients,
    list_projects,
    project_pipeline,
    remove_client_tag,
    search_crm,
    set_project_status,
    tags_for_client,
)
from ..db import audit
from ..email import list_emails, notify
from ..invoices import list_invoices, money
from ..payment_plans import list_payment_plans
from ..portal import enable_portal, portal_url, regenerate_portal_token
from ..project_tasks import add_task, delete_task, list_tasks, task_progress, toggle_task
from ..questionnaires import list_questionnaires
from ..referral_rewards import credit_balance, list_credits, redeem_credit
from ..referrals import referral_code_for, referral_link
from ..scheduler import list_appointments
from .deps import db_conn, render, settings_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


# ── Search ──────────────────────────────────────────────────────────────────


@router.get("/search")
def search(request: Request, q: str = ""):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        results = search_crm(conn, auth.tenant["id"], q)
    return render(request, "crm/search.html", auth=auth, q=q.strip(),
                  clients=results["clients"], projects=results["projects"])


# ── Clients ─────────────────────────────────────────────────────────────────


@router.get("/clients")
def clients_list(request: Request, tag: str = ""):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"], tag=tag or None)
        tags = all_tags(conn, auth.tenant["id"])
    return render(request, "crm/clients.html", auth=auth, clients=clients, tags=tags, active_tag=tag)


def _csv_safe(value) -> str:
    """Neutralize CSV formula injection — a cell starting with = + - @ (or a control
    char) is treated as a formula by spreadsheets; prefix a quote so it stays text.
    Client names/tags are owner-entered, but better safe."""
    s = str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/clients/export.csv")
def clients_export(request: Request, tag: str = ""):
    """Export the client book as CSV (name, contact, tags, projects, lifetime value),
    honoring the active tag filter — e.g. export just the 'vip' clients."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"], tag=tag or None)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["name", "email", "phone", "tags", "projects", "lifetime_value"])
    for c in clients:
        writer.writerow([_csv_safe(x) for x in (
            c["name"], c.get("email") or "", c.get("phone") or "",
            " ".join(c.get("tags") or []), c["project_count"], f"{c['lifetime_cents'] / 100:.2f}",
        )])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="clients.csv"'})


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


# ── segment broadcast: one message to everyone in a tag (literal path before /{id}) ──


@router.get("/clients/broadcast")
def clients_broadcast_compose(request: Request, tag: str = ""):
    """Compose one message to everyone in a tag — a deliberate segment. Pre-filled from
    the 'Announcement / broadcast' template; {client} stays a visible placeholder and is
    filled per recipient on send."""
    seg = tag.strip()
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if not seg:                                  # broadcast is always to a chosen segment
            return RedirectResponse("/clients", status_code=303)
        recipients = [c for c in list_clients(conn, auth.tenant["id"], tag=seg)
                      if (c.get("email") or "").strip()]
        tpl = messaging.get_template(conn, auth.tenant["id"], "broadcast")
    studio = auth.tenant.get("name", "your studio")
    # fill {studio} for the preview but leave {client} as a placeholder (filled per send)
    return render(request, "crm/client_broadcast.html", auth=auth, tag=seg,
                  recipients=recipients, subject=messaging.fill(tpl["subject"], {"studio": studio}),
                  body=messaging.fill(tpl["body"], {"studio": studio}))


@router.post("/clients/broadcast")
def clients_broadcast_send(request: Request, tag: str = Form(""),
                           subject: str = Form(""), body: str = Form("")):
    seg = tag.strip()
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        sent = 0
        if seg and (subject.strip() or body.strip()):
            studio = auth.tenant.get("name", "your studio")
            for c in list_clients(conn, auth.tenant["id"], tag=seg):
                to = (c.get("email") or "").strip()
                if not to:                           # skip segment members with no email
                    continue
                ctx = {"client": c["name"], "studio": studio}
                notify(conn, settings, to=to, subject=messaging.fill(subject.strip(), ctx),
                       body=messaging.fill(body, ctx), tenant_id=auth.tenant["id"])
                sent += 1
            if sent:
                audit(conn, actor="owner", action="segment.emailed",
                      tenant_id=auth.tenant["id"], detail=f"{seg} · {sent} recipient(s)")
                conn.commit()
    return RedirectResponse(f"/clients?tag={quote(seg)}", status_code=303)


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
        timeline = client_timeline(conn, auth.tenant["id"], client_id)
        tags = tags_for_client(conn, auth.tenant["id"], client_id)
        ref_code = referral_code_for(conn, auth.tenant["id"], client_id)
        balance = credit_balance(conn, auth.tenant["id"], client_id)
        credits = list_credits(conn, auth.tenant["id"], client_id)
        # Messages we've sent this client (recipient-scoped so the per-client history
        # isn't truncated by tenant-wide email volume) — the in-app record.
        addr = (client.get("email") or "").strip()
        messages = list_emails(conn, auth.tenant["id"], to_addr=addr) if addr else []
    settings = settings_of(request)
    portal_link = portal_url(settings, client["portal_token"]) \
        if client.get("portal_token") else None
    refer_link = referral_link(settings, auth.tenant["slug"], ref_code) if ref_code else None
    for c in credits:
        c["amount_display"] = money(c["amount_cents"])
    return render(request, "crm/client_detail.html", auth=auth, client=client,
                  projects=projects, timeline=timeline, tags=tags, portal_link=portal_link,
                  refer_link=refer_link, credits=credits, messages=messages,
                  credit_balance_display=money(balance), credit_balance=balance)


@router.get("/clients/{client_id}/email")
def client_email_compose(request: Request, client_id: int):
    """Compose a personal email to the client — pre-filled from the 'Reply to an inquiry'
    template (customizable under Email templates). The studio edits and sends it here, so
    the message, signature, and record all stay inside Hestia."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        client = get_client(conn, auth.tenant["id"], client_id)
        if not client:
            return RedirectResponse("/clients", status_code=303)
        if not (client.get("email") or "").strip():     # nothing to send to
            return RedirectResponse(f"/clients/{client_id}", status_code=303)
        ctx = {"client": client["name"], "studio": auth.tenant.get("name", "your studio")}
        draft = messaging.render(conn, auth.tenant["id"], "inquiry_reply", ctx)
    return render(request, "crm/client_email.html", auth=auth, client=client,
                  subject=draft["subject"], body=draft["body"])


@router.post("/clients/{client_id}/email")
def client_email_send(request: Request, client_id: int,
                      subject: str = Form(""), body: str = Form("")):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        client = get_client(conn, auth.tenant["id"], client_id)
        if not client:
            return RedirectResponse("/clients", status_code=303)
        to = (client.get("email") or "").strip()
        if to and (subject.strip() or body.strip()):
            notify(conn, settings, to=to, subject=subject.strip(), body=body,
                   tenant_id=auth.tenant["id"])      # signed=True → studio signature appended
            audit(conn, actor="owner", action="client.emailed", tenant_id=auth.tenant["id"],
                  detail=f"{client['name']} · {subject.strip()[:80]}")
            conn.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/tags")
def client_add_tag(request: Request, client_id: int, tag: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        add_client_tag(conn, auth.tenant["id"], client_id, tag)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/tags/delete")
def client_remove_tag(request: Request, client_id: int, tag: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        remove_client_tag(conn, auth.tenant["id"], client_id, tag)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/portal")
def client_portal_enable(request: Request, client_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        token = enable_portal(conn, auth.tenant["id"], client_id)
        if token:
            audit(conn, actor="owner", action="client.portal_enabled",
                  tenant_id=auth.tenant["id"], detail=f"client #{client_id}")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/portal/regenerate")
def client_portal_regenerate(request: Request, client_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        token = regenerate_portal_token(conn, auth.tenant["id"], client_id)
        if token:
            audit(conn, actor="owner", action="client.portal_rotated",
                  tenant_id=auth.tenant["id"], detail=f"client #{client_id}")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/credits/{credit_id}/redeem")
def client_credit_redeem(request: Request, client_id: int, credit_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if redeem_credit(conn, auth.tenant["id"], credit_id):
            audit(conn, actor="owner", action="referral.credit_redeemed",
                  tenant_id=auth.tenant["id"], detail=f"credit #{credit_id}")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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


@router.get("/pipeline")
def pipeline(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        stages = project_pipeline(conn, auth.tenant["id"])
    return render(request, "crm/pipeline.html", auth=auth, stages=stages)


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
        invoices = list_invoices(conn, auth.tenant["id"], project_id=project_id,
                                 standalone_only=True)
        plans = list_payment_plans(conn, auth.tenant["id"], project_id=project_id)
        contracts = list_contracts(conn, auth.tenant["id"], project_id=project_id)
        questionnaires = list_questionnaires(conn, auth.tenant["id"], project_id=project_id)
        appointments = list_appointments(conn, auth.tenant["id"], project_id=project_id)
        packs = list_packs(conn, auth.tenant["id"], project_id=project_id)
        recipes = recipes_for(project["shoot_type"])
        tasks = list_tasks(conn, auth.tenant["id"], project_id)
        progress = task_progress(conn, auth.tenant["id"], project_id)
        referred_by = get_client(conn, auth.tenant["id"], project["referred_by_client_id"]) \
            if project.get("referred_by_client_id") else None
    return render(request, "crm/project_detail.html", auth=auth, project=project,
                  galleries=galleries, invoices=invoices, plans=plans, contracts=contracts,
                  questionnaires=questionnaires, appointments=appointments, packs=packs,
                  recipes=recipes, statuses=PROJECT_STATUSES, referred_by=referred_by,
                  tasks=tasks, task_progress=progress)


@router.post("/projects/{project_id}/status")
def project_status(request: Request, project_id: int, status: str = Form(...)):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_project_status(conn, auth.tenant["id"], project_id, status)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks")
def project_task_add(request: Request, project_id: int, label: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if get_project(conn, auth.tenant["id"], project_id):   # only on a project you own
            add_task(conn, tenant_id=auth.tenant["id"], project_id=project_id, label=label)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/toggle")
def project_task_toggle(request: Request, project_id: int, task_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        toggle_task(conn, auth.tenant["id"], task_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/delete")
def project_task_delete(request: Request, project_id: int, task_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_task(conn, auth.tenant["id"], task_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

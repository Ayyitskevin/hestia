"""CRM routes — clients and projects (studio-OS backbone)."""

from __future__ import annotations

import csv
import io
import re
from urllib.parse import quote

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response

from .. import messaging
from ..checklists import apply_checklist
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
    import_clients,
    list_clients,
    list_projects,
    project_pipeline,
    remove_client_tag,
    search_crm,
    set_project_status,
    tags_for_client,
)
from ..csv_export import csv_response
from ..db import audit
from ..email import list_emails, notify
from ..invoices import client_statement, list_invoices, money
from ..payment_plans import list_payment_plans
from ..portal import enable_portal, portal_url, regenerate_portal_token
from ..project_files import (
    add_project_file,
    delete_project_file,
    get_project_file,
    list_project_files,
)
from ..project_tasks import add_task, delete_task, list_tasks, task_progress, toggle_task
from ..questionnaires import list_questionnaires
from ..referral_rewards import credit_balance, list_credits, redeem_credit
from ..referrals import referral_code_for, referral_link
from ..scheduler import list_appointments
from .deps import db_conn, render, settings_of, storage_of, tenant_user

router = APIRouter()




# ── Search ──────────────────────────────────────────────────────────────────


@router.get("/search")
def search(request: Request, q: str = ""):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        results = search_crm(conn, auth.tenant["id"], q)
    return render(request, "crm/search.html", auth=auth, q=q.strip(),
                  clients=results["clients"], projects=results["projects"])


# ── Clients ─────────────────────────────────────────────────────────────────


@router.get("/clients")
def clients_list(request: Request, tag: str = ""):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"], tag=tag or None)
        tags = all_tags(conn, auth.tenant["id"])
    return render(request, "crm/clients.html", auth=auth, clients=clients, tags=tags, active_tag=tag)


@router.get("/clients/export.csv")
def clients_export(request: Request, tag: str = ""):
    """Export the client book as CSV (name, contact, tags, projects, lifetime value),
    honoring the active tag filter — e.g. export just the 'vip' clients."""
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"], tag=tag or None)
    rows = (
        (
            client["name"],
            client.get("email") or "",
            client.get("phone") or "",
            " ".join(client.get("tags") or []),
            client["project_count"],
            f"{client['lifetime_cents'] / 100:.2f}",
        )
        for client in clients
    )
    return csv_response(
        "clients.csv",
        ["name", "email", "phone", "tags", "projects", "lifetime_value"],
        rows,
    )


_CSV_FIELDS = ("name", "email", "phone", "tags", "notes")

# Header labels other tools export, mapped to our fields — so a CSV migrated straight
# from another CRM imports without the user renaming columns first.
_HEADER_SYNONYMS = {
    "name": "name", "full name": "name", "full_name": "name", "fullname": "name",
    "client": "name", "client name": "name", "contact": "name", "contact name": "name",
    "first name": "name",
    "email": "email", "email address": "email", "e-mail": "email", "mail": "email",
    "phone": "phone", "phone number": "phone", "mobile": "phone", "cell": "phone",
    "telephone": "phone", "tel": "phone",
    "tags": "tags", "tag": "tags", "labels": "tags", "label": "tags",
    "notes": "notes", "note": "notes", "comment": "notes", "comments": "notes",
}

_MAX_IMPORT_BYTES = 5_000_000


def _parse_client_csv(text: str) -> list[dict]:
    """Parse client-import CSV text into row dicts. Row 0 is treated as a header only when
    it actually names a ``name`` column (via common synonyms like 'Full Name' / 'First
    Name') and holds no email-looking value — otherwise the file is read positionally as
    name, email, phone, tags, notes. That way a header-less export, Hestia's own export,
    and a foreign tool's differently-labelled export all import correctly (and a real data
    row whose first cell happens to equal a field name isn't mistaken for a header). The
    ``tags`` cell splits on commas/whitespace; fully blank lines are ignored. Raises
    csv.Error on a malformed/binary file, which the caller turns into a friendly message."""
    records = [r for r in csv.reader(io.StringIO(text)) if any((c or "").strip() for c in r)]
    if not records:
        return []
    head = [c.strip().lower() for c in records[0]]
    head_map = {_HEADER_SYNONYMS[h]: i for i, h in enumerate(head) if h in _HEADER_SYNONYMS}
    # a header must identify the (required) name column and carry no data-looking value
    if "name" in head_map and not any("@" in c for c in records[0]):
        field_map, data = head_map, records[1:]
    else:
        field_map, data = {f: i for i, f in enumerate(_CSV_FIELDS)}, records

    def cell(rec: list[str], field: str) -> str:
        i = field_map.get(field)
        return rec[i].strip() if i is not None and i < len(rec) else ""

    rows = []
    for rec in data:
        tags_raw = cell(rec, "tags")
        tags = tags_raw.replace(",", " ").split() if tags_raw else []
        rows.append({"name": cell(rec, "name"), "email": cell(rec, "email"),
                     "phone": cell(rec, "phone"), "notes": cell(rec, "notes"), "tags": tags})
    return rows


@router.get("/clients/import")
def clients_import_form(request: Request):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
    return render(request, "crm/clients_import.html", auth=auth, summary=None, error=None)


@router.post("/clients/import")
async def clients_import(request: Request, file: UploadFile = File(...)):
    # Authenticate BEFORE touching the body — an anonymous (cookieless) POST is
    # CSRF-exempt, so reading/parsing first would let it force an unbounded read.
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        raw = await file.read()
        if len(raw) > _MAX_IMPORT_BYTES:
            return render(request, "crm/clients_import.html", auth=auth, summary=None,
                          error=f"That file is too large (limit {_MAX_IMPORT_BYTES // 1_000_000} MB).")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        try:
            rows = _parse_client_csv(text)
        except csv.Error:
            rows = None
        if rows is None:
            return render(request, "crm/clients_import.html", auth=auth, summary=None,
                          error="That file didn't look like a CSV — please upload a .csv export.")
        summary = import_clients(conn, tenant_id=auth.tenant["id"], rows=rows)
        if summary["imported"]:
            audit(conn, actor="owner", action="clients.imported", tenant_id=auth.tenant["id"],
                  detail=(f"{summary['imported']} imported · {summary['skipped_duplicate']} dupes "
                          f"· {summary['skipped_blank']} blank"))
            conn.commit()
    return render(request, "crm/clients_import.html", auth=auth, summary=summary)


@router.get("/clients/new")
def client_new(request: Request):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
    return render(request, "crm/client_new.html", auth=auth)


@router.post("/clients")
def client_create(request: Request, name: str = Form(...), email: str = Form(""),
                  phone: str = Form(""), notes: str = Form("")):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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


@router.get("/clients/{client_id}/statement")
def client_statement_page(request: Request, client_id: int):
    """A printable account statement — everything billed to the client and what's
    outstanding. Read-only; the studio shares or prints it for the client."""
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        client = get_client(conn, auth.tenant["id"], client_id)
        if not client:
            return RedirectResponse("/clients", status_code=303)
        statement = client_statement(conn, auth.tenant["id"], client_id)
    return render(request, "crm/client_statement.html", auth=auth, client=client,
                  statement=statement)


@router.get("/clients/{client_id}/email")
def client_email_compose(request: Request, client_id: int, template: str = "inquiry_reply"):
    """Compose a personal email to the client, starting from one of the studio's saved
    templates (customizable under Email templates). Only general-purpose templates — those
    that render fully from the client + studio names — are offered, so the draft never
    carries a raw {token}. The studio picks one, edits, and sends here, so the message,
    signature, and record all stay inside Hestia."""
    kind = template if messaging.is_general_template(template) else "inquiry_reply"
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        client = get_client(conn, auth.tenant["id"], client_id)
        if not client:
            return RedirectResponse("/clients", status_code=303)
        if not (client.get("email") or "").strip():     # nothing to send to
            return RedirectResponse(f"/clients/{client_id}", status_code=303)
        ctx = {"client": client["name"], "studio": auth.tenant.get("name", "your studio")}
        draft = messaging.render(conn, auth.tenant["id"], kind, ctx)
    return render(request, "crm/client_email.html", auth=auth, client=client,
                  subject=draft["subject"], body=draft["body"],
                  templates=messaging.general_template_choices(), selected=kind)


@router.post("/clients/{client_id}/email")
def client_email_send(request: Request, client_id: int,
                      subject: str = Form(""), body: str = Form("")):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        add_client_tag(conn, auth.tenant["id"], client_id, tag)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/tags/delete")
def client_remove_tag(request: Request, client_id: int, tag: str = Form("")):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        remove_client_tag(conn, auth.tenant["id"], client_id, tag)
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.post("/clients/{client_id}/portal")
def client_portal_enable(request: Request, client_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "crm/projects.html", auth=auth, projects=projects,
                  statuses=PROJECT_STATUSES)


@router.get("/pipeline")
def pipeline(request: Request):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        stages = project_pipeline(conn, auth.tenant["id"])
    return render(request, "crm/pipeline.html", auth=auth, stages=stages)


@router.get("/projects/new")
def project_new(request: Request, client_id: int | None = None):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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
        auth = tenant_user(request, conn)
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
        files = list_project_files(conn, auth.tenant["id"], project_id)
        referred_by = get_client(conn, auth.tenant["id"], project["referred_by_client_id"]) \
            if project.get("referred_by_client_id") else None
    return render(request, "crm/project_detail.html", auth=auth, project=project,
                  galleries=galleries, invoices=invoices, plans=plans, contracts=contracts,
                  questionnaires=questionnaires, appointments=appointments, packs=packs,
                  recipes=recipes, statuses=PROJECT_STATUSES, referred_by=referred_by,
                  tasks=tasks, task_progress=progress, files=files)


@router.post("/projects/{project_id}/status")
def project_status(request: Request, project_id: int, status: str = Form(...)):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_project_status(conn, auth.tenant["id"], project_id, status)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks")
def project_task_add(request: Request, project_id: int, label: str = Form("")):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if get_project(conn, auth.tenant["id"], project_id):   # only on a project you own
            add_task(conn, tenant_id=auth.tenant["id"], project_id=project_id, label=label)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/toggle")
def project_task_toggle(request: Request, project_id: int, task_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        toggle_task(conn, auth.tenant["id"], task_id, project_id=project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/tasks/{task_id}/delete")
def project_task_delete(request: Request, project_id: int, task_id: int):
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_task(conn, auth.tenant["id"], task_id, project_id=project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/files")
async def project_file_upload(request: Request, project_id: int, file: UploadFile = File(...)):
    """Attach a reference file to a project (owner-only)."""
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if file.filename:
            add_project_file(conn, storage, tenant_id=auth.tenant["id"], project_id=project_id,
                             filename=file.filename, fileobj=file.file,
                             content_type=file.content_type or "application/octet-stream")
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.get("/projects/{project_id}/files/{file_id}")
def project_file_download(request: Request, project_id: int, file_id: int):
    """Download an attached file — always as an attachment (never rendered inline on our
    origin), and only one this studio owns."""
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        f = get_project_file(conn, auth.tenant["id"], file_id)
        if not f or f["project_id"] != project_id:
            return RedirectResponse(f"/projects/{project_id}", status_code=303)
    try:
        data = storage.open(f["storage_key"])
    except FileNotFoundError:
        return Response(status_code=404)
    ascii_name = re.sub(r'[\r\n"\\/]+', "", f.get("filename") or "").strip()
    ascii_name = ascii_name.encode("ascii", "ignore").decode().strip() or f"file-{file_id}"
    return Response(content=data, media_type=f["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{ascii_name}"'})


@router.post("/projects/{project_id}/files/{file_id}/delete")
def project_file_delete(request: Request, project_id: int, file_id: int):
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        delete_project_file(conn, storage, auth.tenant["id"], file_id, project_id=project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)


@router.post("/projects/{project_id}/apply-checklist")
def project_apply_checklist(request: Request, project_id: int):
    """Copy the studio's checklist template for this project's shoot type onto its tasks.
    Idempotent — tasks already present are skipped."""
    with db_conn(request) as conn:
        auth = tenant_user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        if get_project(conn, auth.tenant["id"], project_id):   # only on a project you own
            apply_checklist(conn, auth.tenant["id"], project_id)
    return RedirectResponse(f"/projects/{project_id}", status_code=303)

"""Payment plan routes (studio side) — deposit/balance schedules and tracking.

Installments are invoices, so the client pays each at the existing ``/pay/{token}``
link with the same idempotent settle path; these routes only create the schedule
and surface progress.
"""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import messaging
from ..auth import context_from_session
from ..crm import list_clients, list_projects
from ..db import audit
from ..email import notify
from ..invoices import invoice_public_url, send_invoice
from ..payment_plans import (
    create_payment_plan,
    deposit_balance_installments,
    get_payment_plan,
    list_payment_plans,
    void_payment_plan,
)
from .deps import db_conn, render, settings_of

router = APIRouter(prefix="/payment-plans")


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    try:
        dollars = float(raw.replace("$", "").replace(",", "").strip())
        # 'inf'/'nan' parse but overflow round() to int — treat non-finite as zero.
        return int(round(dollars * 100)) if math.isfinite(dollars) else 0
    except (ValueError, AttributeError):
        return 0


@router.get("")
def plans_list(request: Request):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        plans = list_payment_plans(conn, auth.tenant["id"])
    return render(request, "payment_plans/plans.html", auth=auth, plans=plans)


@router.get("/new")
def plan_new(request: Request, project_id: int | None = None, client_id: int | None = None):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        clients = list_clients(conn, auth.tenant["id"])
        projects = list_projects(conn, auth.tenant["id"])
    return render(request, "payment_plans/plan_new.html", auth=auth, clients=clients,
                  projects=projects, preselect_project=project_id, preselect_client=client_id)


@router.post("")
def plan_create(request: Request, title: str = Form(...), total: str = Form("0"),
                deposit: str = Form("0"), balance_due_date: str = Form(""),
                client_id: str = Form(""), project_id: str = Form("")):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        installments = deposit_balance_installments(
            total_cents=_to_cents(total), deposit_cents=_to_cents(deposit),
            balance_due_date=balance_due_date.strip(),
        )
        plan = create_payment_plan(
            conn, settings_of(request), tenant_id=auth.tenant["id"], title=title,
            installments=installments,
            client_id=int(client_id) if client_id.strip().isdigit() else None,
            project_id=int(project_id) if project_id.strip().isdigit() else None,
        )
    return RedirectResponse(f"/payment-plans/{plan['id']}", status_code=303)


@router.get("/{plan_id}")
def plan_detail(request: Request, plan_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        plan = get_payment_plan(conn, auth.tenant["id"], plan_id)
        if not plan:
            return RedirectResponse("/payment-plans", status_code=303)
    settings = settings_of(request)
    pay_urls = {i["id"]: invoice_public_url(settings, i["token"]) for i in plan["installments"]}
    return render(request, "payment_plans/plan_detail.html", auth=auth, plan=plan, pay_urls=pay_urls)


@router.post("/{plan_id}/send")
def plan_send(request: Request, plan_id: int):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        plan = get_payment_plan(conn, auth.tenant["id"], plan_id)
        if not plan:
            return RedirectResponse("/payment-plans", status_code=303)
        # Mark every still-unpaid installment as sent, then email the whole schedule once.
        unpaid = [i for i in plan["installments"] if i["status"] in ("draft", "sent")]
        for inst in unpaid:
            send_invoice(conn, auth.tenant["id"], inst["id"])
        to = plan.get("client_email")
        if to and unpaid:
            studio = auth.tenant.get("name", "your photographer")
            lines = [
                f"- {i['title']}: {i['amount_display']}"
                + (f" (due {i['due_date']})" if i["due_date"] else "")
                + f"\n  {invoice_public_url(settings, i['token'])}"
                for i in unpaid
            ]
            ctx = {"client": plan.get("client_name") or "there", "studio": studio,
                   "title": plan["title"], "total": plan["total_display"],
                   "schedule": "\n\n".join(lines)}
            msg = messaging.render(conn, auth.tenant["id"], "payment_schedule", ctx)
            notify(conn, settings, to=to, tenant_id=auth.tenant["id"],
                   subject=msg["subject"], body=msg["body"])
        audit(conn, actor="owner", action="payment_plan.sent", tenant_id=auth.tenant["id"],
              detail=f"{plan['title']} · {len(unpaid)} installments")
        conn.commit()
    return RedirectResponse(f"/payment-plans/{plan_id}", status_code=303)


@router.post("/{plan_id}/void")
def plan_void(request: Request, plan_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        void_payment_plan(conn, auth.tenant["id"], plan_id)
    return RedirectResponse(f"/payment-plans/{plan_id}", status_code=303)

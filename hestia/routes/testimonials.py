"""Testimonials — the public submit link plus the owner management hub.

Public ``/t/{token}`` is anonymous (no session → the CSRF dependency is a
no-op for it, the same way the public studio inquiry works); the owner hub under
``/settings/testimonials`` is session-authenticated and CSRF-protected.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from .. import messaging
from ..auth import context_from_session
from ..crm import get_client, list_clients
from ..email import notify
from ..ratelimit import enforce
from ..tenants import get_tenant
from ..testimonials import (
    get_by_token,
    list_testimonials,
    request_testimonial,
    set_status,
    submit_testimonial,
    testimonial_public_url,
)
from .deps import db_conn, render, settings_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


# ── Owner hub ───────────────────────────────────────────────────────────────


@router.get("/settings/testimonials")
def manage(request: Request):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        items = list_testimonials(conn, auth.tenant["id"])
        clients = list_clients(conn, auth.tenant["id"])
    for t in items:  # the share link for pending requests
        t["url"] = testimonial_public_url(settings, t["token"])
    return render(request, "testimonials/manage.html", auth=auth, items=items, clients=clients)


@router.post("/settings/testimonials/request")
def request_one(request: Request, client_id: str = Form(""), author_name: str = Form("")):
    settings = settings_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        raw = int(client_id) if client_id.strip().isdigit() else None
        client = get_client(conn, auth.tenant["id"], raw) if raw else None
        cid = client["id"] if client else None  # only store a client we actually own
        name = author_name.strip() or (client["name"] if client else "")
        t = request_testimonial(conn, tenant_id=auth.tenant["id"], client_id=cid, author_name=name)
        if client and client.get("email"):  # send the client their link
            url = testimonial_public_url(settings, t["token"])
            studio = auth.tenant.get("name", "your photographer")
            ctx = {"client": client["name"], "studio": studio, "review_url": url}
            msg = messaging.render(conn, auth.tenant["id"], "review_request", ctx)
            notify(conn, settings, to=client["email"], tenant_id=auth.tenant["id"],
                   subject=msg["subject"], body=msg["body"])
    return RedirectResponse("/settings/testimonials", status_code=303)


@router.post("/testimonials/{testimonial_id}/feature")
def feature(request: Request, testimonial_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_status(conn, auth.tenant["id"], testimonial_id, "featured")
    return RedirectResponse("/settings/testimonials", status_code=303)


@router.post("/testimonials/{testimonial_id}/hide")
def hide(request: Request, testimonial_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_status(conn, auth.tenant["id"], testimonial_id, "hidden")
    return RedirectResponse("/settings/testimonials", status_code=303)


# ── Public submit ───────────────────────────────────────────────────────────


@router.get("/t/{token}")
def public_form(request: Request, token: str):
    with db_conn(request) as conn:
        t = get_by_token(conn, token)
        if not t:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, t["tenant_id"])
    return render(request, "testimonials/submit.html", auth=None, t=t, tenant=tenant,
                  done=t["status"] != "requested")


@router.post("/t/{token}")
def public_submit(request: Request, token: str, rating: str = Form("5"),
                  body: str = Form(""), author_name: str = Form("")):
    enforce(request, "inquiry")
    with db_conn(request) as conn:
        t = get_by_token(conn, token)
        if not t:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, t["tenant_id"])
        submit_testimonial(conn, token, rating=rating, body=body, author_name=author_name)
    return render(request, "testimonials/thanks.html", auth=None, tenant=tenant)

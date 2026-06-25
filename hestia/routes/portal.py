"""Public client portal — one branded link, read-only, links out to the flows."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..portal import assemble_portal, get_client_by_portal_token
from .deps import db_conn, render, settings_of

router = APIRouter()


@router.get("/portal/{token}")
def client_portal(request: Request, token: str):
    with db_conn(request) as conn:
        client = get_client_by_portal_token(conn, token)
        if not client:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        data = assemble_portal(conn, settings_of(request), client)
    return render(request, "portal/portal.html", auth=None, client=client, **data)

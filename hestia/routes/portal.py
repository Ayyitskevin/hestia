"""Public client portal — one branded link, read-only, links out to the flows."""

from __future__ import annotations

import re

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..dashboard import owner_digest_recipient  # resolves the studio's inbox
from ..email import notify
from ..portal import assemble_portal, get_client_by_portal_token
from ..project_files import get_client_file
from ..ratelimit import enforce
from .deps import db_conn, render, settings_of, storage_of

router = APIRouter()


@router.get("/portal/{token}")
def client_portal(request: Request, token: str, sent: str = ""):
    with db_conn(request) as conn:
        client = get_client_by_portal_token(conn, token)
        if not client:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        data = assemble_portal(conn, settings_of(request), client)
    return render(request, "portal/portal.html", auth=None, client=client, sent=bool(sent), **data)


@router.get("/portal/{token}/files/{file_id}")
def portal_file_download(request: Request, token: str, file_id: int):
    """Download a file the studio shared on one of this client's projects — always as an
    attachment, and only a file belonging to THIS client's projects (the token gates it)."""
    storage = storage_of(request)
    with db_conn(request) as conn:
        client = get_client_by_portal_token(conn, token)
        if not client:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        f = get_client_file(conn, client["tenant_id"], client["id"], file_id)
        if not f:                                      # not this client's file → 404
            return render(request, "offer_missing.html", auth=None, status_code=404)
    try:
        data = storage.open(f["storage_key"])
    except FileNotFoundError:
        return Response(status_code=404)
    name = re.sub(r'[\r\n"\\/]+', "", f.get("filename") or "").strip()
    name = name.encode("ascii", "ignore").decode().strip() or f"file-{file_id}"
    return Response(content=data, media_type=f["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/portal/{token}/message")
def portal_message(request: Request, token: str, message: str = Form("")):
    """An existing client messages their studio from the portal — delivered as an owner
    alert (unsigned, like the lead alert). No-op if the message is empty."""
    enforce(request, "inquiry")
    settings = settings_of(request)
    with db_conn(request) as conn:
        client = get_client_by_portal_token(conn, token)
        if not client:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        body = message.strip()
        to = owner_digest_recipient(conn, client["tenant_id"])
        if body and to:
            trow = conn.execute(
                "SELECT name FROM tenants WHERE id = ?", (client["tenant_id"],)
            ).fetchone()
            studio = (trow["name"] if trow else "") or "your studio"
            reply_to = client.get("email") or "(no email on file)"
            notify(conn, settings, to=to, tenant_id=client["tenant_id"], signed=False,
                   subject=f"Message from {client['name']}",
                   body=(f"{client['name']} sent {studio} a message via their client portal:\n\n"
                         f"{body}\n\nReply to them at: {reply_to}"))
            conn.commit()
    return RedirectResponse(f"/portal/{token}?sent=1", status_code=303)

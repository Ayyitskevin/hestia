"""Public album review — the client opens an unguessable link, pages through the
AI-arranged spreads, and approves. No login; the review token is the gate. Read-only
except the one-way approve, so (like delivery) this router carries no CSRF."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..albums import (
    album_spreads_display,
    approve_album,
    get_album_by_review_token,
    request_album_changes,
)
from ..galleries import safe_inline_type
from ..tenants import get_tenant
from .deps import db_conn, image_response, render, storage_of

router = APIRouter()


@router.get("/a/{token}")
def album_review_page(request: Request, token: str):
    with db_conn(request) as conn:
        album = get_album_by_review_token(conn, token)
        if not album:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        tenant = get_tenant(conn, album["tenant_id"])
        spreads = album_spreads_display(
            conn, album, lambda img: f"/a/{token}/photo/{img['id']}/view")
    return render(request, "albums/album_review.html", auth=None, album=album,
                  tenant=tenant, spreads=spreads, token=token)


@router.post("/a/{token}/approve")
def album_review_approve(request: Request, token: str):
    with db_conn(request) as conn:
        if not get_album_by_review_token(conn, token):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        approve_album(conn, token)          # idempotent: a second approval is a no-op
    return RedirectResponse(f"/a/{token}", status_code=303)


@router.post("/a/{token}/request-changes")
def album_review_request_changes(request: Request, token: str, note: str = Form("")):
    with db_conn(request) as conn:
        if not get_album_by_review_token(conn, token):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        request_album_changes(conn, token, note)
    return RedirectResponse(f"/a/{token}", status_code=303)


@router.get("/a/{token}/photo/{image_id}/view")
def album_review_photo(request: Request, token: str, image_id: int):
    """Serve a spread frame INLINE, scoped to this album's gallery — so a review token can't
    reach another gallery's files, and the album loads regardless of the gallery's publish
    state (it doesn't go through the publish-gated /media route)."""
    storage = storage_of(request)
    with db_conn(request) as conn:
        album = get_album_by_review_token(conn, token)
        if not album:
            return Response(status_code=404)
        # hidden = 0: a culled frame must not be served to the client even if its id lingers
        # in the album's spreads (e.g. the owner culled it after generating the album).
        img = conn.execute(
            "SELECT * FROM images WHERE id = ? AND gallery_id = ? AND tenant_id = ? AND hidden = 0",
            (image_id, album["gallery_id"], album["tenant_id"]),
        ).fetchone()
        if not img:
            return Response(status_code=404)
        img = dict(img)
    # Serve the small browse thumbnail when present (the review page shows every spread
    # frame); fall back to the original, clamped to a safe image type so a stored
    # text/html "image" can't execute. Streams from disk and caches hard (image_response).
    if img.get("thumb_key"):
        return image_response(request, storage, img["thumb_key"], media_type="image/jpeg")
    return image_response(request, storage, img["storage_key"],
                          media_type=safe_inline_type(img["content_type"]))

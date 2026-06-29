"""Serve gallery images from storage, with tenant/publish-aware access control."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..auth import context_from_session
from ..galleries import safe_inline_type
from .deps import db_conn, storage_of

router = APIRouter()


@router.get("/media/{key:path}")
def serve_media(request: Request, key: str):
    storage = storage_of(request)
    with db_conn(request) as conn:
        img = conn.execute(
            "SELECT i.*, g.status AS gallery_status FROM images i "
            "JOIN galleries g ON g.id = i.gallery_id WHERE i.storage_key = ?",
            (key,),
        ).fetchone()
        if not img:
            return Response(status_code=404)
        # Fast public path: a published, non-hidden frame is served to anyone.
        allowed = img["gallery_status"] == "published" and not img["hidden"]
        if not allowed:
            # Otherwise only the owner may view it — an unpublished gallery, or a hidden
            # (culled) frame the owner is still managing. A hidden frame must NOT be served
            # to a client even though its /media/ key is predictable, or culling leaks.
            auth = context_from_session(conn, request)
            allowed = bool(auth and auth.tenant and auth.tenant["id"] == img["tenant_id"])
        if not allowed:
            return Response(status_code=403)
    try:
        data = storage.open(key)
    except FileNotFoundError:
        return Response(status_code=404)
    # Served inline → clamp to a safe image type so a stored text/html "image" can't
    # execute as a page on our origin (the content_type is client-supplied at upload).
    return Response(content=data, media_type=safe_inline_type(img["content_type"]))

"""Serve gallery images from storage, with tenant/publish-aware access control."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..auth import context_from_session
from ..galleries import safe_inline_type
from .deps import db_conn, image_response, storage_of

router = APIRouter()


@router.get("/media/{key:path}")
def serve_media(request: Request, key: str, s: str = ""):
    storage = storage_of(request)
    with db_conn(request) as conn:
        if "/" in key:
            # Legacy storage-key path (<tenant>/<gallery>/<image>.<ext>): the ids are
            # sequential and the tenant id leaks in public <img src>, so this path is
            # enumerable and is therefore OWNER-ONLY. Client image URLs use the token
            # branch below (storage.image_url), which is unguessable.
            img = conn.execute(
                "SELECT * FROM images WHERE storage_key = ?", (key,)
            ).fetchone()
            if not img:
                return Response(status_code=404)
            auth = context_from_session(conn, request)
            if not (auth and auth.tenant and auth.tenant["id"] == img["tenant_id"]):
                return Response(status_code=403)
        else:
            # Capability token: unguessable per-image. Public iff the gallery is
            # published and the frame isn't hidden (a culled frame never leaks to a
            # client); otherwise only the owner may view it.
            img = conn.execute(
                "SELECT i.*, g.status AS gallery_status FROM images i "
                "JOIN galleries g ON g.id = i.gallery_id AND g.tenant_id = i.tenant_id "
                "WHERE i.access_token = ?",
                (key,),
            ).fetchone()
            if not img:
                return Response(status_code=404)
            allowed = img["gallery_status"] == "published" and not img["hidden"]
            if not allowed:
                auth = context_from_session(conn, request)
                allowed = bool(auth and auth.tenant and auth.tenant["id"] == img["tenant_id"])
            if not allowed:
                return Response(status_code=403)
        storage_key, content_type = img["storage_key"], img["content_type"]
        thumb_key = img["thumb_key"] if "thumb_key" in img.keys() else None
    # ?s=t serves the small browse thumbnail when one exists (image/jpeg is inherently
    # safe); otherwise the original, clamped to a safe image type so a stored text/html
    # "image" can't execute as a page on our origin (content_type is client-supplied).
    if s == "t" and thumb_key:
        return image_response(request, storage, thumb_key, media_type="image/jpeg")
    return image_response(request, storage, storage_key,
                          media_type=safe_inline_type(content_type))

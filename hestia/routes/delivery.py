"""Public digital delivery — the client downloads their gallery via an unguessable
link: each original individually, or the whole set as one zip. No login; the token
is the gate. Read-only, so this router carries no CSRF (like the media route)."""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from ..delivery import delivery_expired, get_gallery_by_delivery_token, iter_zip
from ..galleries import (
    list_images,
    record_gallery_download,
    record_gallery_view,
    safe_inline_type,
)
from ..proofing import favorite_image_ids
from ..ratelimit import enforce
from ..tenants import get_tenant
from .deps import db_conn, render, storage_of

router = APIRouter()


def _content_disposition(name: str, fallback: str) -> str:
    """An RFC 6266 Content-Disposition for a download. Response headers are latin-1,
    so a non-Latin-1 filename (CJK, Cyrillic, emoji — routine for real clients) would
    crash the response; we send an ASCII-safe ``filename`` plus the true UTF-8 name in
    ``filename*`` so it neither 500s nor loses the original name."""
    cleaned = re.sub(r'[\r\n"\\/]+', "", name or "").strip() or fallback
    ascii_name = cleaned.encode("ascii", "ignore").decode().strip() or fallback
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(cleaned)}"


@router.get("/d/{token}")
def delivery_page(request: Request, token: str):
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        if delivery_expired(conn, gallery):
            tenant = get_tenant(conn, gallery["tenant_id"])
            return render(request, "delivery_expired.html", auth=None, gallery=gallery,
                          tenant=tenant, status_code=410)
        images = list_images(conn, gallery["id"], include_hidden=False)
        record_gallery_view(conn, gallery["id"])          # the client opened their gallery
        favorites = favorite_image_ids(conn, gallery["id"])   # frames they hearted in proofing
    total_bytes = sum(img.get("bytes") or 0 for img in images)
    return render(request, "delivery.html", auth=None, gallery=gallery, images=images,
                  token=token, total_bytes=total_bytes, favorites=favorites)


@router.get("/d/{token}/all.zip")
def delivery_zip(request: Request, token: str):
    enforce(request, "download")  # the zip is the one expensive public read — rate-limit it
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        if delivery_expired(conn, gallery):
            return Response(status_code=410)              # link past its expiry date
        images = list_images(conn, gallery["id"], include_hidden=False)
        if images:
            record_gallery_download(conn, gallery["id"])  # whole-set zip download
    if not images:
        return Response(status_code=404)
    # Stream the archive (bounded memory) so a multi-GB wedding gallery can't OOM us.
    name = (gallery.get("slug") or gallery.get("title") or "gallery")
    return StreamingResponse(
        iter_zip(storage, images), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{name}.zip", "gallery.zip")})


@router.get("/d/{token}/favorites.zip")
def delivery_favorites_zip(request: Request, token: str):
    """Just the frames the client hearted in proofing, as one zip — the proofing → selects
    payoff. Registered before /{image_id} (which is int-typed anyway). 404 if no favorites."""
    enforce(request, "download")
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        if delivery_expired(conn, gallery):
            return Response(status_code=410)              # link past its expiry date
        favs = favorite_image_ids(conn, gallery["id"])
        images = [im for im in list_images(conn, gallery["id"], include_hidden=False)
                  if im["id"] in favs]
        if images:
            record_gallery_download(conn, gallery["id"])  # favorites zip download
    if not images:
        return Response(status_code=404)
    name = (gallery.get("slug") or gallery.get("title") or "gallery")
    return StreamingResponse(
        iter_zip(storage, images), media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(f"{name}-favorites.zip",
                                                             "favorites.zip")})


@router.get("/d/{token}/{image_id}")
def delivery_file(request: Request, token: str, image_id: int):
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        if delivery_expired(conn, gallery):
            return Response(status_code=410)              # link past its expiry date
        # scope the image to THIS gallery — a token can't reach another gallery's files.
        # hidden = 0: a culled frame is excluded from delivery, so it can't be pulled by id either.
        img = conn.execute(
            "SELECT * FROM images WHERE id = ? AND gallery_id = ? AND hidden = 0",
            (image_id, gallery["id"]),
        ).fetchone()
        if not img:
            return Response(status_code=404)
        img = dict(img)
        record_gallery_download(conn, gallery["id"])      # individual original download
    try:
        data = storage.open(img["storage_key"])
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(content=data, media_type=img["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": _content_disposition(
                        img.get("filename", ""), f"image-{image_id}")})


@router.get("/d/{token}/{image_id}/view")
def delivery_view(request: Request, token: str, image_id: int):
    """Same token-scoped image, served INLINE — used for the thumbnails on the
    download page (no Content-Disposition, so the browser renders it in-place)."""
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        if delivery_expired(conn, gallery):
            return Response(status_code=410)              # link past its expiry date
        img = conn.execute(
            "SELECT * FROM images WHERE id = ? AND gallery_id = ? AND hidden = 0",
            (image_id, gallery["id"]),
        ).fetchone()
        if not img:
            return Response(status_code=404)
        img = dict(img)
    try:
        data = storage.open(img["storage_key"])
    except FileNotFoundError:
        return Response(status_code=404)
    # Inline render, so clamp to a safe image type — a stored text/html "image" must
    # not execute as a page on our origin.
    return Response(content=data, media_type=safe_inline_type(img["content_type"]))

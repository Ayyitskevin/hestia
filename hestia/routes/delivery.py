"""Public digital delivery — the client downloads their gallery via an unguessable
link: each original individually, or the whole set as one zip. No login; the token
is the gate. Read-only, so this router carries no CSRF (like the media route)."""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import Response

from ..delivery import get_gallery_by_delivery_token, zip_gallery
from ..galleries import list_images
from .deps import db_conn, render, storage_of

router = APIRouter()


def _safe_filename(name: str, fallback: str) -> str:
    """A header-safe download filename (strip quotes/control chars/path bits)."""
    cleaned = re.sub(r'[\r\n"\\/]+', "", name or "").strip()
    return cleaned or fallback


@router.get("/d/{token}")
def delivery_page(request: Request, token: str):
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        images = list_images(conn, gallery["id"])
    total_bytes = sum(img.get("bytes") or 0 for img in images)
    return render(request, "delivery.html", auth=None, gallery=gallery, images=images,
                  token=token, total_bytes=total_bytes)


@router.get("/d/{token}/all.zip")
def delivery_zip(request: Request, token: str):
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        images = list_images(conn, gallery["id"])
    if not images:
        return Response(status_code=404)
    data = zip_gallery(storage, images)
    name = _safe_filename(gallery.get("slug") or gallery.get("title", ""), "gallery")
    return Response(content=data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})


@router.get("/d/{token}/{image_id}")
def delivery_file(request: Request, token: str, image_id: int):
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        # scope the image to THIS gallery — a token can't reach another gallery's files
        img = conn.execute(
            "SELECT * FROM images WHERE id = ? AND gallery_id = ?",
            (image_id, gallery["id"]),
        ).fetchone()
        if not img:
            return Response(status_code=404)
        img = dict(img)
    try:
        data = storage.open(img["storage_key"])
    except FileNotFoundError:
        return Response(status_code=404)
    name = _safe_filename(img.get("filename", ""), f"image-{image_id}")
    return Response(content=data, media_type=img["content_type"] or "application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/d/{token}/{image_id}/view")
def delivery_view(request: Request, token: str, image_id: int):
    """Same token-scoped image, served INLINE — used for the thumbnails on the
    download page (no Content-Disposition, so the browser renders it in-place)."""
    storage = storage_of(request)
    with db_conn(request) as conn:
        gallery = get_gallery_by_delivery_token(conn, token)
        if not gallery:
            return Response(status_code=404)
        img = conn.execute(
            "SELECT * FROM images WHERE id = ? AND gallery_id = ?",
            (image_id, gallery["id"]),
        ).fetchone()
        if not img:
            return Response(status_code=404)
        img = dict(img)
    try:
        data = storage.open(img["storage_key"])
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(content=data, media_type=img["content_type"] or "application/octet-stream")

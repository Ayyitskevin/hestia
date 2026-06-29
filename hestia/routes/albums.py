"""Album routes — generate drafted spreads from a gallery, then view them."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..albums import (
    AlbumError,
    album_review_url,
    album_spreads_display,
    enable_album_review,
    generate_album,
    get_album,
    set_spread_hero,
)
from ..auth import context_from_session
from ..galleries import get_gallery
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of, storage_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.post("/galleries/{gallery_id}/album")
def album_generate(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        try:
            album = generate_album(conn, settings_of(request), tenant=tenant, gallery=gallery)
        except AlbumError:
            return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)
    return RedirectResponse(f"/albums/{album['id']}", status_code=303)


@router.post("/albums/{album_id}/spreads/{position}/hero/{image_id}")
def album_spread_hero(request: Request, album_id: int, position: int, image_id: int):
    """Override which frame leads a spread — the photographer's pick over the AI's."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        set_spread_hero(conn, auth.tenant["id"], album_id, position, image_id)
    return RedirectResponse(f"/albums/{album_id}", status_code=303)


@router.post("/albums/{album_id}/share")
def album_share(request: Request, album_id: int):
    """Mint (idempotently) the album's client review link, then return to the album."""
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        enable_album_review(conn, auth.tenant["id"], album_id)
    return RedirectResponse(f"/albums/{album_id}", status_code=303)


@router.get("/albums/{album_id}")
def album_view(request: Request, album_id: int):
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        album = get_album(conn, auth.tenant["id"], album_id)
        if not album:
            return RedirectResponse("/galleries", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], album["gallery_id"])
        # Owner view serves frames through the authorized /media path.
        spreads = album_spreads_display(conn, album,
                                        lambda img: storage.public_path(img["storage_key"]))
    review_url = (album_review_url(settings_of(request), album["review_token"])
                  if album.get("review_token") else None)
    return render(request, "albums/album.html", auth=auth, album=album, gallery=gallery,
                  spreads=spreads, review_url=review_url)

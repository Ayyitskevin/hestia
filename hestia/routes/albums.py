"""Album routes — generate drafted spreads from a gallery, then view them."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..albums import AlbumError, generate_album, get_album
from ..auth import context_from_session
from ..galleries import get_gallery, get_image
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
        # Resolve image ids → display dicts for the template.
        spreads = []
        for sp in album["spreads"]:
            photos = []
            for iid in sp["photo_ids"]:
                img = get_image(conn, auth.tenant["id"], iid)
                if img:
                    photos.append({"id": iid, "url": storage.public_path(img["storage_key"]),
                                   "filename": img["filename"], "is_hero": iid == sp["hero_image_id"]})
            spreads.append({"position": sp["position"], "photos": photos})
    return render(request, "albums/album.html", auth=auth, album=album, gallery=gallery, spreads=spreads)

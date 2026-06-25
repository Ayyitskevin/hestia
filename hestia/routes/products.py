"""Product photography routes — generate marketplace variants, then view them."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..galleries import get_gallery, get_image
from ..products import generate_product_set, get_product_set
from ..tenants import get_tenant
from .deps import db_conn, render, settings_of, storage_of

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


@router.post("/galleries/{gallery_id}/products")
def products_generate(request: Request, gallery_id: int):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], gallery_id)
        if not gallery:
            return RedirectResponse("/galleries", status_code=303)
        tenant = get_tenant(conn, auth.tenant["id"])
        try:
            pset = generate_product_set(conn, settings_of(request), tenant=tenant,
                                        gallery=gallery, storage=storage_of(request))
        except ValueError:
            return RedirectResponse(f"/galleries/{gallery_id}", status_code=303)
    return RedirectResponse(f"/products/{pset['id']}", status_code=303)


@router.get("/products/{set_id}")
def products_view(request: Request, set_id: int):
    storage = storage_of(request)
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        pset = get_product_set(conn, auth.tenant["id"], set_id)
        if not pset:
            return RedirectResponse("/galleries", status_code=303)
        gallery = get_gallery(conn, auth.tenant["id"], pset["gallery_id"])
        # Group variants by source image, with a resolved source thumbnail.
        by_image: dict[int, dict] = {}
        for v in pset["variants"]:
            grp = by_image.setdefault(v["image_id"], {"filename": v["filename"], "url": None, "variants": []})
            grp["variants"].append(v)
        for iid, grp in by_image.items():
            img = get_image(conn, auth.tenant["id"], iid)
            if img:
                grp["url"] = storage.public_path(img["storage_key"])
        groups = list(by_image.values())
    return render(request, "products/set.html", auth=auth, pset=pset, gallery=gallery, groups=groups)

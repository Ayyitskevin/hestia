"""Client-facing pages: the public offer (the magic-moment payoff) + gallery view."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..galleries import get_gallery_by_slug, get_image, list_images
from ..proofing import (
    add_comment,
    comments_by_image,
    favorite_image_ids,
    toggle_favorite,
)
from ..ratelimit import enforce
from ..sales import get_offer_by_token
from ..storage import Storage
from ..tenants import get_tenant_by_slug
from .deps import db_conn, render, storage_of

router = APIRouter()


def _resolve_unlocked(conn, request: Request, slug: str, gallery_slug: str):
    """Return (tenant, gallery, unlocked) for a published gallery, or (.., None, ..)."""
    tenant = get_tenant_by_slug(conn, slug)
    gallery = get_gallery_by_slug(conn, tenant["id"], gallery_slug) if tenant else None
    if not tenant or not gallery or gallery["status"] != "published":
        return tenant, None, False
    unlocked = not gallery["pin"] or request.cookies.get(f"g_{gallery['id']}") == gallery["pin"]
    return tenant, gallery, unlocked


def _hero_urls(conn, storage: Storage, tenant_id: str, image_ids: list[int]) -> list[dict]:
    out = []
    for iid in image_ids:
        img = get_image(conn, tenant_id, iid)
        if img:
            out.append({"id": img["id"], "url": storage.public_path(img["storage_key"]),
                        "filename": img["filename"]})
    return out


@router.get("/s/{slug}/{token}")
def public_offer(request: Request, slug: str, token: str):
    """Public, shareable client offer — what the photographer sends the client."""
    with db_conn(request) as conn:
        offer = get_offer_by_token(conn, token)
        tenant = get_tenant_by_slug(conn, slug)
        if not offer or not tenant or offer["tenant_id"] != tenant["id"]:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        heroes = _hero_urls(conn, storage_of(request), tenant["id"], offer["hero_images"])
    return render(request, "offer.html", auth=None, offer=offer, tenant=tenant, heroes=heroes)


@router.get("/g/{slug}/{gallery_slug}")
def client_gallery(request: Request, slug: str, gallery_slug: str):
    """Client gallery delivery. PIN-gated when the gallery has a PIN."""
    favorites: set[int] = set()
    comments: dict[int, list] = {}
    with db_conn(request) as conn:
        tenant, gallery, unlocked = _resolve_unlocked(conn, request, slug, gallery_slug)
        if not gallery:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        images = list_images(conn, gallery["id"]) if unlocked else []
        offer = None
        if unlocked:
            from ..sales import get_offer_for_gallery, offer_public_url
            from .deps import settings_of
            o = get_offer_for_gallery(conn, tenant["id"], gallery["id"])
            if o:
                offer = offer_public_url(settings_of(request), slug, o["token"])
            favorites = favorite_image_ids(conn, gallery["id"])
            comments = comments_by_image(conn, gallery["id"])
    return render(request, "client_gallery.html", auth=None, tenant=tenant, gallery=gallery,
                  images=images, unlocked=unlocked, storage=storage_of(request), offer_url=offer,
                  favorites=favorites, comments=comments)


@router.post("/g/{slug}/{gallery_slug}/favorite/{image_id}")
def client_favorite(request: Request, slug: str, gallery_slug: str, image_id: int):
    """Toggle a client favorite (only on an unlocked gallery)."""
    enforce(request, "checkout")
    with db_conn(request) as conn:
        tenant, gallery, unlocked = _resolve_unlocked(conn, request, slug, gallery_slug)
        if gallery and unlocked:
            toggle_favorite(conn, tenant_id=tenant["id"], gallery_id=gallery["id"], image_id=image_id)
    return RedirectResponse(f"/g/{slug}/{gallery_slug}#img-{image_id}", status_code=303)


@router.post("/g/{slug}/{gallery_slug}/comment/{image_id}")
def client_comment(request: Request, slug: str, gallery_slug: str, image_id: int,
                   body: str = Form(""), author_name: str = Form("")):
    """Leave a client comment on a frame (only on an unlocked gallery)."""
    enforce(request, "checkout")
    with db_conn(request) as conn:
        tenant, gallery, unlocked = _resolve_unlocked(conn, request, slug, gallery_slug)
        if gallery and unlocked:
            add_comment(conn, tenant_id=tenant["id"], gallery_id=gallery["id"],
                        image_id=image_id, body=body, author_name=author_name)
    return RedirectResponse(f"/g/{slug}/{gallery_slug}#img-{image_id}", status_code=303)


@router.post("/g/{slug}/{gallery_slug}/pin")
def client_gallery_pin(request: Request, slug: str, gallery_slug: str, pin: str = Form(...)):
    with db_conn(request) as conn:
        tenant = get_tenant_by_slug(conn, slug)
        gallery = get_gallery_by_slug(conn, tenant["id"], gallery_slug) if tenant else None
        if not gallery:
            return RedirectResponse("/", status_code=303)
    resp = RedirectResponse(f"/g/{slug}/{gallery_slug}", status_code=303)
    if gallery["pin"] and pin.strip() == gallery["pin"]:
        resp.set_cookie(f"g_{gallery['id']}", pin.strip(), httponly=True, samesite="lax", max_age=86400)
    return resp

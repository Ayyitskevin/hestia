"""Client-facing pages: the public offer (the magic-moment payoff) + gallery view."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..campaigns import discount_bundle, get_active_campaign
from ..dashboard import owner_digest_recipient
from ..email import notify
from ..galleries import (
    get_gallery_by_slug,
    get_image,
    list_images,
    record_gallery_view,
    submit_selections,
)
from ..orders import create_order
from ..proofing import (
    add_comment,
    comments_by_image,
    favorite_count,
    favorite_image_ids,
    list_favorites,
    selection_packet,
    toggle_favorite,
)
from ..ratelimit import enforce
from ..sales import favorites_package, get_offer_by_token, get_tenant_catalog
from ..storage import Storage
from ..tenants import get_tenant_by_slug
from ..vision import alt_text_map
from .deps import db_conn, render, settings_of, storage_of

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
        if img and not img["hidden"]:        # a culled frame never resurfaces on the public offer
            out.append({"id": img["id"], "url": storage.image_url(img),
                        "filename": img["filename"]})
    return out


def _offer_gallery_owned(conn, offer: dict, tenant_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM galleries WHERE id = ? AND tenant_id = ?",
        (offer["gallery_id"], tenant_id),
    ).fetchone() is not None


@router.get("/s/{slug}/{token}")
def public_offer(request: Request, slug: str, token: str):
    """Public, shareable client offer — what the photographer sends the client."""
    fav_thumbs: list[dict] = []
    fav_pkg = None
    with db_conn(request) as conn:
        offer = get_offer_by_token(conn, token)
        tenant = get_tenant_by_slug(conn, slug)
        if (
            not offer or not tenant or offer["tenant_id"] != tenant["id"]
            or not _offer_gallery_owned(conn, offer, tenant["id"])
        ):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        storage = storage_of(request)
        heroes = _hero_urls(conn, storage, tenant["id"], offer["hero_images"])
        # Live: auto-curate a package from the photos the client hearted. Drop any the
        # owner has since culled (hidden) — they're gone from the gallery, so they can't
        # reappear in the offer's thumbnails or its favorites-package count.
        favs = [i for i in list_favorites(conn, tenant["id"], offer["gallery_id"]) if not i["hidden"]]
        fav_thumbs = [{"url": storage.image_url(i), "filename": i["filename"]}
                      for i in favs]
        fav_pkg = favorites_package(
            len(favs),
            price_per_print_cents=get_tenant_catalog(conn, tenant["id"])["favorite_print_cents"],
        )
        # Live: a running sale discounts the prices and adds urgency.
        campaign = get_active_campaign(conn, offer["gallery_id"], tenant_id=tenant["id"])
        pct = campaign["discount_pct"] if campaign else 0
        if pct:
            offer["bundles"] = [discount_bundle(b, pct) for b in offer["bundles"]]
            offer["total_cents"] = sum(b["price_cents"] for b in offer["bundles"])
            if fav_pkg:
                fav_pkg = discount_bundle(fav_pkg, pct)
    return render(request, "offer.html", auth=None, offer=offer, tenant=tenant, heroes=heroes,
                  fav_thumbs=fav_thumbs, fav_pkg=fav_pkg, campaign=campaign)


@router.post("/s/{slug}/{token}/order")
def offer_order(request: Request, slug: str, token: str, sku: str = Form(...)):
    """Reserve a bundle → create an order + invoice → send the client to checkout."""
    enforce(request, "checkout")
    settings = settings_of(request)
    with db_conn(request) as conn:
        offer = get_offer_by_token(conn, token)
        tenant = get_tenant_by_slug(conn, slug)
        if (
            not offer or not tenant or offer["tenant_id"] != tenant["id"]
            or not _offer_gallery_owned(conn, offer, tenant["id"])
        ):
            return render(request, "offer_missing.html", auth=None, status_code=404)
        result = create_order(conn, settings, tenant=dict(tenant), offer=offer, sku=sku)
        if not result:
            return RedirectResponse(f"/s/{slug}/{token}", status_code=303)
        invoice_token = result["invoice"]["token"]
    return RedirectResponse(f"/pay/{invoice_token}", status_code=303)


@router.get("/g/{slug}/{gallery_slug}")
def client_gallery(request: Request, slug: str, gallery_slug: str):
    """Client gallery delivery. PIN-gated when the gallery has a PIN."""
    favorites: set[int] = set()
    comments: dict[int, list] = {}
    packet = None
    alts: dict[int, str] = {}
    with db_conn(request) as conn:
        tenant, gallery, unlocked = _resolve_unlocked(conn, request, slug, gallery_slug)
        if not gallery:
            return render(request, "offer_missing.html", auth=None, status_code=404)
        images = list_images(conn, gallery["id"], include_hidden=False) if unlocked else []
        offer = None
        if unlocked:
            record_gallery_view(conn, gallery["id"])      # client opened their proofing gallery
            from ..sales import get_offer_for_gallery, offer_public_url
            from .deps import settings_of
            o = get_offer_for_gallery(conn, tenant["id"], gallery["id"])
            if o:
                offer = offer_public_url(settings_of(request), slug, o["token"])
            favorites = favorite_image_ids(conn, gallery["id"], tenant_id=tenant["id"])
            comments = comments_by_image(conn, gallery["id"], tenant_id=tenant["id"])
            packet = selection_packet(conn, tenant["id"], gallery["id"])
            alts = alt_text_map(conn, gallery["id"])      # AI captions for accessible/SEO alt text
    return render(request, "client_gallery.html", auth=None, tenant=tenant, gallery=gallery,
                  images=images, unlocked=unlocked, storage=storage_of(request), offer_url=offer,
                  favorites=favorites, comments=comments, selection_packet=packet, alts=alts)


@router.post("/g/{slug}/{gallery_slug}/favorite/{image_id}")
def client_favorite(request: Request, slug: str, gallery_slug: str, image_id: int):
    """Toggle a client favorite (only on an unlocked gallery)."""
    enforce(request, "checkout")
    with db_conn(request) as conn:
        tenant, gallery, unlocked = _resolve_unlocked(conn, request, slug, gallery_slug)
        if gallery and unlocked:
            toggle_favorite(conn, tenant_id=tenant["id"], gallery_id=gallery["id"], image_id=image_id)
    return RedirectResponse(f"/g/{slug}/{gallery_slug}#img-{image_id}", status_code=303)


@router.post("/g/{slug}/{gallery_slug}/submit")
def client_submit_selections(request: Request, slug: str, gallery_slug: str):
    """Client finalizes their proofing picks ("I'm done — send these"). On the first
    submit, notify the owner once so they can build the album/print offer promptly.
    Idempotent: a re-submit is a no-op (no re-stamp, no second email)."""
    enforce(request, "checkout")
    settings = settings_of(request)
    with db_conn(request) as conn:
        tenant, gallery, unlocked = _resolve_unlocked(conn, request, slug, gallery_slug)
        if gallery and unlocked and submit_selections(
            conn, tenant_id=tenant["id"], gallery_id=gallery["id"]
        ):
            count = favorite_count(conn, gallery["id"], tenant_id=tenant["id"])
            to = owner_digest_recipient(conn, tenant["id"])
            if to:                                  # notify() also no-ops on empty recipient
                who = gallery["client_name"] or "Your client"
                plural = "" if count == 1 else "s"
                notify(
                    conn, settings, to=to, tenant_id=tenant["id"], signed=False,
                    subject=f"{who} sent their favorites from {gallery['title']}",
                    body=(f"{who} finalized their selections on \"{gallery['title']}\" — "
                          f"{count} favorite{plural}.\n\nReview them and build the album or "
                          f"print offer here:\n{settings.public_url}/galleries/{gallery['id']}"),
                )
            conn.commit()
    return RedirectResponse(f"/g/{slug}/{gallery_slug}", status_code=303)


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

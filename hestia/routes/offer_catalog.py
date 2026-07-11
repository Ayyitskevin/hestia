"""Owner routes — per-studio print/album offer catalog pricing."""

from __future__ import annotations

import math

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..auth import context_from_session
from ..sales import (
    CATALOG_SKUS,
    DEFAULT_CATALOG,
    get_tenant_catalog,
    set_tenant_catalog,
)
from .deps import db_conn, render

router = APIRouter()


def _user(request: Request, conn):
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def _to_cents(raw: str) -> int:
    try:
        cents = float(raw.replace("$", "").replace(",", "").strip()) * 100
        return int(round(cents)) if math.isfinite(cents) else 0
    except (ValueError, AttributeError, OverflowError):
        return 0


@router.get("/settings/offers")
def offer_catalog_settings(request: Request, saved: str = ""):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        catalog = get_tenant_catalog(conn, auth.tenant["id"])
        rows = []
        for sku in CATALOG_SKUS:
            item = catalog["items"][sku]
            default = DEFAULT_CATALOG[sku]
            rows.append(
                {
                    "sku": sku,
                    "label": default["name"],
                    "name": item["name"],
                    "blurb": item["blurb"],
                    "price_dollars": f"{item['price_cents'] / 100:.2f}".rstrip("0").rstrip("."),
                    "enabled": item["enabled"],
                }
            )
        fav_cents = catalog["favorite_print_cents"]
    return render(
        request,
        "studio/offer_catalog.html",
        auth=auth,
        rows=rows,
        favorite_print_dollars=f"{fav_cents / 100:.2f}".rstrip("0").rstrip("."),
        saved=bool(saved),
    )


@router.post("/settings/offers")
def offer_catalog_save(
    request: Request,
    favorite_print: str = Form("15"),
    print_set_name: str = Form(""),
    print_set_blurb: str = Form(""),
    print_set_price: str = Form(""),
    print_set_enabled: str = Form(""),
    wall_art_name: str = Form(""),
    wall_art_blurb: str = Form(""),
    wall_art_price: str = Form(""),
    wall_art_enabled: str = Form(""),
    album_name: str = Form(""),
    album_blurb: str = Form(""),
    album_price: str = Form(""),
    album_enabled: str = Form(""),
    gift_box_name: str = Form(""),
    gift_box_blurb: str = Form(""),
    gift_box_price: str = Form(""),
    gift_box_enabled: str = Form(""),
):
    with db_conn(request) as conn:
        auth = _user(request, conn)
        if not auth:
            return RedirectResponse("/login", status_code=303)
        items = {
            "print_set": {
                "name": print_set_name,
                "blurb": print_set_blurb,
                "price_cents": _to_cents(print_set_price)
                or DEFAULT_CATALOG["print_set"]["price_cents"],
                "enabled": bool(print_set_enabled),
            },
            "wall_art": {
                "name": wall_art_name,
                "blurb": wall_art_blurb,
                "price_cents": _to_cents(wall_art_price)
                or DEFAULT_CATALOG["wall_art"]["price_cents"],
                "enabled": bool(wall_art_enabled),
            },
            "album": {
                "name": album_name,
                "blurb": album_blurb,
                "price_cents": _to_cents(album_price) or DEFAULT_CATALOG["album"]["price_cents"],
                "enabled": bool(album_enabled),
            },
            "gift_box": {
                "name": gift_box_name,
                "blurb": gift_box_blurb,
                "price_cents": _to_cents(gift_box_price)
                or DEFAULT_CATALOG["gift_box"]["price_cents"],
                "enabled": bool(gift_box_enabled),
            },
        }
        set_tenant_catalog(
            conn,
            auth.tenant["id"],
            items=items,
            favorite_print_cents=_to_cents(favorite_print) or 1500,
        )
        conn.commit()
    return RedirectResponse("/settings/offers?saved=1", status_code=303)

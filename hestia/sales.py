"""Sales module — turn a gallery into client revenue (essence of Plutus, in-app).

Builds print/album bundles from the gallery's vision summary and shoot type, and
mints ONE shareable client offer per gallery. The offer is **idempotent**: the
public token is created once and reused on every re-run, so re-processing a
gallery never produces a second client link — the exact gap the research found in
the real Plutus (`storefront.py` INSERTs a fresh token every call).

Stripe checkout is implemented (see :mod:`hestia.payments` and
:mod:`hestia.routes.webhooks`); the offer page renders and shares today, and
collects real money when the Stripe backend is configured.
"""

from __future__ import annotations

import json
import math
import sqlite3

from .config import Settings
from .crypto import new_session_token
from .features import FeatureFlags
from .ownership import owned_gallery_id

CATALOG_SKUS = ("print_set", "wall_art", "album", "gift_box")

# Default catalog (cents). Studios override per-SKU via ``offer_catalog_json``.
DEFAULT_CATALOG = {
    "print_set": {
        "name": "Signature Print Set",
        "category": "print",
        "blurb": "Ten archival 8×10 prints of your favorite frames.",
        "price_cents": 12000,
        "enabled": True,
    },
    "wall_art": {
        "name": "Wall Art Canvas",
        "category": "canvas",
        "blurb": "A gallery-wrapped 24×36 canvas of your hero image.",
        "price_cents": 22000,
        "enabled": True,
    },
    "album": {
        "name": "Heirloom Album",
        "category": "album",
        "blurb": "A 30-page lay-flat album, designed from your gallery.",
        "price_cents": 45000,
        "enabled": True,
    },
    "gift_box": {
        "name": "Gift Collection",
        "category": "gift",
        "blurb": "Mini prints + cards to share with family.",
        "price_cents": 8000,
        "enabled": True,
    },
}

FAVORITE_PRINT_CENTS_DEFAULT = 1500
_MAX_PRICE_CENTS = 10_000_000


def _clamp_price_cents(raw) -> int:
    try:
        cents = int(raw)
        return max(0, min(_MAX_PRICE_CENTS, cents)) if math.isfinite(cents) else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _normalize_catalog_entry(sku: str, raw: dict | None) -> dict:
    base = DEFAULT_CATALOG[sku]
    raw = raw if isinstance(raw, dict) else {}
    price = _clamp_price_cents(raw.get("price_cents", base["price_cents"]))
    if price <= 0:
        price = base["price_cents"]
    name = str(raw.get("name") or base["name"]).strip()[:80] or base["name"]
    blurb = str(raw.get("blurb") or base["blurb"]).strip()[:240] or base["blurb"]
    enabled = raw.get("enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in {"1", "true", "yes", "on"}
    return {
        "sku": sku,
        "name": name,
        "category": base["category"],
        "blurb": blurb,
        "price_cents": price,
        "enabled": bool(enabled),
    }


def get_tenant_catalog(conn: sqlite3.Connection, tenant_id: str) -> dict:
    """Merged catalog + per-favorite print price for a studio."""
    row = conn.execute(
        "SELECT offer_catalog_json, favorite_print_cents FROM tenants WHERE id = ?",
        (tenant_id,),
    ).fetchone()
    saved: dict = {}
    if row and row["offer_catalog_json"]:
        try:
            parsed = json.loads(row["offer_catalog_json"])
            if isinstance(parsed, dict):
                saved = parsed
        except (TypeError, ValueError):
            saved = {}
    fav = _clamp_price_cents(row["favorite_print_cents"] if row else FAVORITE_PRINT_CENTS_DEFAULT)
    if fav <= 0:
        fav = FAVORITE_PRINT_CENTS_DEFAULT
    items = {sku: _normalize_catalog_entry(sku, saved.get(sku)) for sku in CATALOG_SKUS}
    return {"items": items, "favorite_print_cents": fav}


def get_tenant_catalog_items(conn: sqlite3.Connection, tenant_id: str) -> dict:
    return get_tenant_catalog(conn, tenant_id)["items"]


def set_tenant_catalog(
    conn: sqlite3.Connection,
    tenant_id: str,
    *,
    items: dict,
    favorite_print_cents: int,
) -> None:
    """Persist studio offer pricing overrides."""
    normalized = {sku: _normalize_catalog_entry(sku, items.get(sku)) for sku in CATALOG_SKUS}
    fav = _clamp_price_cents(favorite_print_cents)
    if fav <= 0:
        fav = FAVORITE_PRINT_CENTS_DEFAULT
    conn.execute(
        "UPDATE tenants SET offer_catalog_json = ?, favorite_print_cents = ? WHERE id = ?",
        (json.dumps(normalized), fav, tenant_id),
    )


def build_bundles(
    flags: FeatureFlags,
    vision_summary: dict,
    *,
    catalog: dict | None = None,
) -> list[dict]:
    """Curate offer bundles from shoot type + vision signal + studio catalog."""
    catalog = catalog or DEFAULT_CATALOG
    hero_n = len(vision_summary.get("hero_image_ids", []))
    keepers = vision_summary.get("keeper_count", 0)
    bundles: list[dict] = []

    if b := _bundle("print_set", catalog=catalog, note=f"{keepers} keeper frames culled for you."):
        bundles.append(b)
    if b := _bundle(
        "wall_art", catalog=catalog, note=f"Built around your top {max(hero_n, 1)} hero shots."
    ):
        bundles.append(b)
    if flags.album_offer:
        if b := _bundle(
            "album",
            catalog=catalog,
            note="Recommended for your shoot type — drafted from the gallery.",
        ):
            bundles.append(b)
    if b := _bundle("gift_box", catalog=catalog, note="A little something for the whole family."):
        bundles.append(b)
    return bundles


def _bundle(sku: str, *, note: str, catalog: dict) -> dict | None:
    p = catalog.get(sku) or DEFAULT_CATALOG[sku]
    if not p.get("enabled", True):
        return None
    price = p["price_cents"]
    return {
        "sku": sku,
        "name": p["name"],
        "category": p["category"],
        "blurb": p["blurb"],
        "note": note,
        "price_cents": price,
        "price": f"${price / 100:,.0f}",
    }


def favorites_package(
    favorite_count: int,
    *,
    price_per_print_cents: int = FAVORITE_PRINT_CENTS_DEFAULT,
) -> dict | None:
    """A ready-to-order print set auto-built from the client's hearted frames.

    Computed live at offer-render time (favorites accrue after the offer is
    minted), so it always reflects the latest picks. Returns None when nothing is
    favorited yet. This is the proofing→sales bridge: the client's own signal,
    turned into a sellable package no point tool can assemble.
    """
    n = max(0, int(favorite_count))
    if n == 0:
        return None
    per = _clamp_price_cents(price_per_print_cents)
    if per <= 0:
        per = FAVORITE_PRINT_CENTS_DEFAULT
    price = n * per
    return {
        "sku": "favorites",
        "name": f"Your Favorites — {n} archival print{'' if n == 1 else 's'}",
        "category": "print",
        "blurb": "The frames you hearted, printed as a curated fine-art set.",
        "note": "Auto-built from the photos you loved.",
        "count": n,
        "price_cents": price,
        "price": f"${price / 100:,.0f}",
    }


# ── Offer persistence (idempotent: one offer/token per gallery) ─────────────


def create_or_update_offer(
    conn: sqlite3.Connection,
    *,
    tenant: dict,
    gallery: dict,
    run_id: int | None,
    vision_summary: dict,
    flags: FeatureFlags,
) -> dict | None:
    if owned_gallery_id(conn, tenant["id"], gallery["id"]) is None:
        return None
    catalog = get_tenant_catalog_items(conn, tenant["id"])
    bundles = build_bundles(flags, vision_summary, catalog=catalog)
    hero_images = vision_summary.get("hero_image_ids", [])
    title = f"{gallery['title']} — print & album collection"

    existing = conn.execute(
        "SELECT * FROM offers WHERE tenant_id = ? AND gallery_id = ?",
        (tenant["id"], gallery["id"]),
    ).fetchone()

    if existing:
        # Idempotent: keep the SAME public token; refresh the curated bundles.
        conn.execute(
            """
            UPDATE offers SET run_id = ?, title = ?, bundles_json = ?,
                   hero_images_json = ?, status = 'active', updated_at = datetime('now')
             WHERE id = ?
            """,
            (run_id, title, json.dumps(bundles), json.dumps(hero_images), existing["id"]),
        )
        offer_id = existing["id"]
    else:
        token = new_session_token()[:28]
        cur = conn.execute(
            """
            INSERT INTO offers (tenant_id, gallery_id, run_id, token, title,
                                bundles_json, hero_images_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant["id"],
                gallery["id"],
                run_id,
                token,
                title,
                json.dumps(bundles),
                json.dumps(hero_images),
            ),
        )
        offer_id = cur.lastrowid
    conn.commit()
    return _offer_row(conn, offer_id)


def _offer_row(conn: sqlite3.Connection, offer_id: int) -> dict:
    row = conn.execute("SELECT * FROM offers WHERE id = ?", (offer_id,)).fetchone()
    return _hydrate(dict(row)) if row else None


def get_offer_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM offers WHERE tenant_id = ? AND gallery_id = ?", (tenant_id, gallery_id)
    ).fetchone()
    return _hydrate(dict(row)) if row else None


def get_offer_by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM offers WHERE token = ?", (token,)).fetchone()
    return _hydrate(dict(row)) if row else None


def _hydrate(row: dict) -> dict:
    row["bundles"] = json.loads(row.pop("bundles_json") or "[]")
    row["hero_images"] = json.loads(row.pop("hero_images_json") or "[]")
    row["total_cents"] = sum(b["price_cents"] for b in row["bundles"])
    return row


def offer_public_url(settings: Settings, tenant_slug: str, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/s/{tenant_slug}/{token}"

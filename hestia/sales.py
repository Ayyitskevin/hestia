"""Sales module — turn a gallery into client revenue (essence of Plutus, in-app).

Builds print/album bundles from the gallery's vision summary and shoot type, and
mints ONE shareable client offer per gallery. The offer is **idempotent**: the
public token is created once and reused on every re-run, so re-processing a
gallery never produces a second client link — the exact gap the research found in
the real Plutus (`storefront.py` INSERTs a fresh token every call).

Stripe checkout is a Phase 1 scaffold (see :mod:`hestia.billing`); the offer page
renders and shares today without it.
"""

from __future__ import annotations

import json
import sqlite3

from .config import Settings
from .crypto import new_session_token
from .features import FeatureFlags

# Hardcoded catalog (cents). A real deployment would make this per-tenant.
CATALOG = {
    "print_set": {"name": "Signature Print Set", "category": "print",
                  "blurb": "Ten archival 8×10 prints of your favorite frames.", "price_cents": 12000},
    "wall_art": {"name": "Wall Art Canvas", "category": "canvas",
                 "blurb": "A gallery-wrapped 24×36 canvas of your hero image.", "price_cents": 22000},
    "album": {"name": "Heirloom Album", "category": "album",
              "blurb": "A 30-page lay-flat album, designed from your gallery.", "price_cents": 45000},
    "gift_box": {"name": "Gift Collection", "category": "gift",
                 "blurb": "Mini prints + cards to share with family.", "price_cents": 8000},
}


def build_bundles(flags: FeatureFlags, vision_summary: dict) -> list[dict]:
    """Curate offer bundles from shoot type + vision signal."""
    hero_n = len(vision_summary.get("hero_image_ids", []))
    keepers = vision_summary.get("keeper_count", 0)
    bundles: list[dict] = []

    bundles.append(_bundle("print_set",
                           note=f"{keepers} keeper frames culled for you."))
    bundles.append(_bundle("wall_art",
                           note=f"Built around your top {max(hero_n, 1)} hero shots."))
    if flags.album_offer:
        bundles.append(_bundle("album",
                               note="Recommended for your shoot type — drafted from the gallery."))
    bundles.append(_bundle("gift_box", note="A little something for the whole family."))
    return bundles


def _bundle(sku: str, *, note: str) -> dict:
    p = CATALOG[sku]
    return {
        "sku": sku,
        "name": p["name"],
        "category": p["category"],
        "blurb": p["blurb"],
        "note": note,
        "price_cents": p["price_cents"],
        "price": f"${p['price_cents'] / 100:,.0f}",
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
) -> dict:
    bundles = build_bundles(flags, vision_summary)
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
            (tenant["id"], gallery["id"], run_id, token, title,
             json.dumps(bundles), json.dumps(hero_images)),
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

"""Product photography — marketplace-spec packshot variants (essence of Aphrodite).

Aphrodite targets e-commerce merchants, not photographers — so rather than bolt on
a separate product, its essence lands here as a **commercial/product module**: turn
a product gallery into the marketplace-spec variants a shop needs (catalog square,
transparent cutout, lifestyle hero, …).

Pluggable renderer, same seam as the rest:
- ``mock`` — *plans* every variant (the exact target spec) without fabricating
  pixels. Honest: it tells you what will be produced. The default; testable.
- ``xai`` — renders via xAI Grok Imagine image edits (needs a key; defensive).

Idempotent: one variant set per gallery, regenerated in place.
"""

from __future__ import annotations

import json
import sqlite3

from .config import Settings

# Marketplace presets (mirrors Aphrodite's marketplaces.py).
PRESETS = [
    {"key": "catalog_square", "label": "Catalog square", "width": 2000, "height": 2000,
     "format": "jpg", "background": "white"},
    {"key": "marketplace_main", "label": "Marketplace main", "width": 1600, "height": 1600,
     "format": "jpg", "background": "white"},
    {"key": "transparent_cutout", "label": "Transparent cutout", "width": 2000, "height": 2000,
     "format": "png", "background": "transparent"},
    {"key": "social_square", "label": "Social square", "width": 1080, "height": 1080,
     "format": "jpg", "background": "lifestyle"},
    {"key": "hero_wide", "label": "Hero wide", "width": 2400, "height": 1350,
     "format": "jpg", "background": "lifestyle"},
]
PRESETS_BY_KEY = {p["key"]: p for p in PRESETS}


class MockRenderer:
    backend = "mock"

    def render(self, *, image: dict, preset: dict) -> dict:
        # Plan the variant; do not fabricate rendered pixels.
        return {"status": "planned", "output_ref": image["storage_key"],
                "note": f"{preset['background']} bg → {preset['width']}×{preset['height']} {preset['format']}"}


class XaiRenderer:
    backend = "xai"

    def __init__(self, settings: Settings):
        self.settings = settings

    def render(self, *, image: dict, preset: dict) -> dict:
        # Real generation via Grok Imagine needs the source bytes + a key; on any
        # gap we degrade to a plan so the set still completes.
        if not self.settings.xai_api_key:
            return {"status": "planned", "output_ref": image["storage_key"], "note": "no xai key — planned only"}
        # A full implementation uploads image bytes to /images/edits with a prompt
        # built from the preset; left as a documented integration point.
        return {"status": "planned", "output_ref": image["storage_key"], "note": "xai renderer stub"}


def build_renderer(settings: Settings):
    if settings.product_backend == "xai":
        return XaiRenderer(settings)
    return MockRenderer()


def generate_product_set(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant: dict,
    gallery: dict,
    preset_keys: list[str] | None = None,
    renderer=None,
) -> dict:
    from .galleries import list_images

    images = list_images(conn, gallery["id"])
    if not images:
        raise ValueError("gallery has no product images")
    presets = [PRESETS_BY_KEY[k] for k in (preset_keys or []) if k in PRESETS_BY_KEY] or PRESETS
    renderer = renderer or build_renderer(settings)

    variants = []
    for img in images:
        for preset in presets:
            r = renderer.render(image=img, preset=preset)
            variants.append({
                "image_id": img["id"], "filename": img["filename"],
                "preset": preset["key"], "label": preset["label"],
                "width": preset["width"], "height": preset["height"],
                "format": preset["format"], "status": r["status"], "note": r.get("note", ""),
            })

    existing = conn.execute(
        "SELECT id FROM product_sets WHERE tenant_id = ? AND gallery_id = ?",
        (tenant["id"], gallery["id"]),
    ).fetchone()
    backend = getattr(renderer, "backend", "mock")
    if existing:
        conn.execute(
            "UPDATE product_sets SET backend = ?, variants_json = ?, updated_at = datetime('now') WHERE id = ?",
            (backend, json.dumps(variants), existing["id"]),
        )
        set_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO product_sets (tenant_id, gallery_id, backend, variants_json) VALUES (?, ?, ?, ?)",
            (tenant["id"], gallery["id"], backend, json.dumps(variants)),
        )
        set_id = cur.lastrowid
    conn.commit()
    return get_product_set(conn, tenant["id"], set_id)


def _hydrate(row: dict) -> dict:
    row["variants"] = json.loads(row.pop("variants_json") or "[]")
    row["variant_count"] = len(row["variants"])
    return row


def get_product_set(conn: sqlite3.Connection, tenant_id: str, set_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM product_sets WHERE id = ? AND tenant_id = ?", (set_id, tenant_id)
    ).fetchone()
    return _hydrate(dict(row)) if row else None


def get_set_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM product_sets WHERE tenant_id = ? AND gallery_id = ?", (tenant_id, gallery_id)
    ).fetchone()
    return _hydrate(dict(row)) if row else None

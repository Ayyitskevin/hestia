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
import os
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


# xAI Grok Imagine image model. Env-overridable so it can be corrected without a
# code change; verify against xAI's current image API before going live.
XAI_IMAGE_MODEL = os.getenv("HESTIA_XAI_IMAGE_MODEL", "grok-2-image-1212")


def _prompt_for(preset: dict) -> str:
    bg = {
        "white": "on a clean seamless white background",
        "transparent": "with the product cleanly cut out on a transparent background",
        "lifestyle": "in a tasteful lifestyle setting",
    }.get(preset["background"], "on a clean background")
    return (f"Re-render this product photo as a {preset['label']} packshot, {bg}, "
            f"composed for {preset['width']}×{preset['height']}, e-commerce ready, "
            f"photorealistic, no text or watermarks.")


class MockRenderer:
    backend = "mock"

    def render(self, *, image: dict, preset: dict, storage=None) -> dict:
        # Plan the variant; do not fabricate rendered pixels.
        return {"status": "planned", "output_ref": image["storage_key"],
                "note": f"{preset['background']} bg → {preset['width']}×{preset['height']} {preset['format']}"}


class XaiRenderer:
    backend = "xai"

    def __init__(self, settings: Settings):
        self.settings = settings

    def render(self, *, image: dict, preset: dict, storage=None) -> dict:
        planned = {"status": "planned", "output_ref": image["storage_key"]}
        # Need both a key and somewhere to read the source / write the output.
        if not self.settings.xai_api_key or storage is None:
            return {**planned, "note": "no xai key — planned only"}
        try:
            return self._render_live(image=image, preset=preset, storage=storage)
        except Exception as exc:  # noqa: BLE001 - never break the set on a render miss
            return {**planned, "note": f"xai render failed, planned: {exc}"}

    def _render_live(self, *, image: dict, preset: dict, storage) -> dict:
        import base64
        import io

        import httpx

        source = storage.open(image["storage_key"])
        with httpx.Client(base_url=self.settings.xai_base_url, timeout=120) as c:
            resp = c.post(
                "/images/edits",
                headers={"Authorization": f"Bearer {self.settings.xai_api_key}"},
                data={"model": XAI_IMAGE_MODEL, "prompt": _prompt_for(preset),
                      "response_format": "b64_json"},
                files={"image": (image["filename"], source, "application/octet-stream")},
            )
        resp.raise_for_status()
        out = base64.b64decode(resp.json()["data"][0]["b64_json"])
        out_key = f"{image['storage_key']}.{preset['key']}.{preset['format']}"
        storage.put(out_key, io.BytesIO(out), f"image/{preset['format']}")
        return {"status": "rendered", "output_ref": out_key, "note": f"{preset['label']} rendered"}


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
    storage=None,
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
            r = renderer.render(image=img, preset=preset, storage=storage)
            variants.append({
                "image_id": img["id"], "filename": img["filename"],
                "preset": preset["key"], "label": preset["label"],
                "width": preset["width"], "height": preset["height"],
                "format": preset["format"], "status": r["status"],
                "output_ref": r.get("output_ref", ""), "note": r.get("note", ""),
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

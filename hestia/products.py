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

import base64
import io
import json
import sqlite3

from PIL import Image, ImageOps

from .config import Settings
from .xai import XaiTransport

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


# xAI accepts image-edit sources as JSON data URIs, which expands the source in
# memory. Keep the provider boundary below its documented 20 MiB image-input cap.
_MAX_SOURCE_BYTES = 20 * 1024 * 1024
_MAX_SOURCE_PIXELS = 60_000_000
_MAX_SOURCE_SIDE = 20_000

# Imagine currently returns at most a 2K image. These ceilings leave generous
# headroom while preventing a malformed Base64 payload or decompression bomb from
# consuming unbounded memory before the result reaches tenant storage.
_MAX_RENDER_BYTES = 20 * 1024 * 1024
_MAX_RENDER_PIXELS = 4096 * 4096
_MAX_RENDER_SIDE = 4096
_MAX_RESPONSE_BYTES = 4 * ((_MAX_RENDER_BYTES + 2) // 3) + 64 * 1024

_MIME_BY_IMAGE_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png"}
_IMAGE_FORMAT_BY_PRESET = {"jpg": "JPEG", "png": "PNG"}


def _inspect_image(
    data: bytes,
    *,
    label: str,
    max_bytes: int,
    max_pixels: int,
    max_side: int,
) -> tuple[str, str]:
    if not data:
        raise ValueError(f"{label} contained no image bytes")
    if len(data) > max_bytes:
        raise ValueError(f"{label} exceeds the {max_bytes}-byte limit")

    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format or ""
            if image_format not in _MIME_BY_IMAGE_FORMAT:
                raise ValueError(f"{label} format {image_format or 'unknown'} is unsupported")
            width, height = image.size
            if (
                width <= 0
                or height <= 0
                or max(width, height) > max_side
                or width * height > max_pixels
            ):
                raise ValueError(f"{label} dimensions {width}x{height} exceed the safe limit")
            # Verify the encoded container first, then reopen and load below to
            # force a full pixel decode. Header or magic-byte checks alone accept
            # truncated and otherwise corrupt images.
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"{label} is not a decodable image") from exc

    return image_format, _MIME_BY_IMAGE_FORMAT[image_format]


def _canonicalize_rendered_image(data: bytes, *, preset: dict) -> tuple[bytes, str]:
    expected_format = _IMAGE_FORMAT_BY_PRESET.get(preset["format"])
    if expected_format is None:
        raise ValueError(f"unsupported product preset format: {preset['format']}")
    target_size = (preset["width"], preset["height"])
    if (
        min(target_size) <= 0
        or max(target_size) > _MAX_RENDER_SIDE
        or target_size[0] * target_size[1] > _MAX_RENDER_PIXELS
    ):
        raise ValueError("product preset dimensions exceed the safe render limit")
    _inspect_image(
        data,
        label="xai image response",
        max_bytes=_MAX_RENDER_BYTES,
        max_pixels=_MAX_RENDER_PIXELS,
        max_side=_MAX_RENDER_SIDE,
    )

    # A single-image xAI edit preserves the source aspect ratio and returns a
    # provider-sized raster (currently 1K/2K), not Hestia's exact marketplace
    # dimensions. Center-crop and resize deterministically, then re-encode into
    # the promised format. This also strips provider metadata.
    canonical = io.BytesIO()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        image = ImageOps.fit(image, target_size, method=Image.Resampling.LANCZOS)
        if expected_format == "JPEG":
            if "A" in image.getbands():
                flattened = Image.new("RGBA", image.size, "white")
                flattened.alpha_composite(image.convert("RGBA"))
                image = flattened
            image.convert("RGB").save(canonical, format="JPEG", quality=95, optimize=True)
        else:
            image = image.convert("RGBA")
            if preset.get("background") == "transparent":
                alpha_min, _ = image.getchannel("A").getextrema()
                if alpha_min == 255:
                    raise ValueError("xai image response has no retained transparent pixels")
            image.save(canonical, format="PNG", optimize=True)
    rendered = canonical.getvalue()
    if not rendered or len(rendered) > _MAX_RENDER_BYTES:
        raise ValueError("canonical xai image response exceeds the decoded size limit")
    return rendered, _MIME_BY_IMAGE_FORMAT[expected_format]


def _decode_rendered_image(encoded: object) -> bytes:
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("xai image response contained no rendered image")
    max_encoded = 4 * ((_MAX_RENDER_BYTES + 2) // 3)
    if len(encoded) > max_encoded:
        raise ValueError("xai image response exceeds the encoded size limit")
    try:
        rendered = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("xai image response was not strict Base64") from exc
    if not rendered:
        raise ValueError("xai image response contained no rendered bytes")
    if len(rendered) > _MAX_RENDER_BYTES:
        raise ValueError("xai image response exceeds the decoded size limit")
    return rendered


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

    def __init__(self, settings: Settings, transport: XaiTransport | None = None):
        self.settings = settings
        self.transport = transport or XaiTransport(settings)
        self._source_cache_key: str | None = None
        self._source_data_uri: str | None = None

    def render(self, *, image: dict, preset: dict, storage=None) -> dict:
        planned = {"status": "planned", "output_ref": image["storage_key"]}
        # Need both a key and somewhere to read the source / write the output.
        if not self.settings.xai_api_key or storage is None:
            return {**planned, "note": "no xai key — planned only"}
        try:
            return self._render_live(image=image, preset=preset, storage=storage)
        except Exception as exc:  # noqa: BLE001 - never break the set on a render miss
            return {**planned, "note": f"xai render failed, planned: {exc}"}

    def _source_uri(self, *, image: dict, storage) -> str:
        storage_key = image["storage_key"]
        if storage_key == self._source_cache_key and self._source_data_uri is not None:
            return self._source_data_uri
        declared_size = image.get("bytes")
        if isinstance(declared_size, int) and declared_size > _MAX_SOURCE_BYTES:
            raise ValueError("xai source metadata exceeds the provider size limit")
        source = storage.open(storage_key)
        _, source_mime = _inspect_image(
            source,
            label="xai source",
            max_bytes=_MAX_SOURCE_BYTES,
            max_pixels=_MAX_SOURCE_PIXELS,
            max_side=_MAX_SOURCE_SIDE,
        )
        data_uri = f"data:{source_mime};base64,{base64.b64encode(source).decode()}"
        self._source_cache_key = storage_key
        self._source_data_uri = data_uri
        return data_uri

    def _render_live(self, *, image: dict, preset: dict, storage) -> dict:
        source_uri = self._source_uri(image=image, storage=storage)
        resp = self.transport.post(
            "/images/edits",
            timeout=120,
            max_response_bytes=_MAX_RESPONSE_BYTES,
            json={
                "model": self.settings.xai_image_model,
                "prompt": _prompt_for(preset),
                "image": {
                    "type": "image_url",
                    "url": source_uri,
                },
                "resolution": "2k",
                "response_format": "b64_json",
            },
        )
        encoded = resp.json()["data"][0]["b64_json"]
        out = _decode_rendered_image(encoded)
        out, output_mime = _canonicalize_rendered_image(out, preset=preset)
        out_key = f"{image['storage_key']}.{preset['key']}.{preset['format']}"
        storage.put(out_key, io.BytesIO(out), output_mime)
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

    images = list_images(conn, gallery["id"], tenant_id=tenant["id"])
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

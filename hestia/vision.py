"""Vision module — understand every frame (the essence of Argus, in-app).

A pluggable provider analyzes each image into keywords, a keeper score, a hero
score, a shot type, and alt text. The ``mock`` provider is deterministic (derived
from the filename) so tests, offers, and demos are stable with no API key; the
``xai`` provider calls xAI Grok vision. Results persist to ``image_analyses``.

This replaces a network hop to a separate Argus service with a function call —
which is the whole point of consolidating the suite into one app.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from .config import Settings
from .storage import Storage

SHOT_TYPES = ["portrait", "candid", "detail", "wide", "group", "landscape", "still-life"]

# A frame is flagged as a likely blink at/above this eyes-closed score, and a
# duplicate cluster keeps only its highest-keeper pick (the rest are culls).
BLINK_THRESHOLD = 0.85
KEEPER_THRESHOLD = 0.7


def content_dup_key(data: bytes) -> str:
    """A content signature for duplicate detection — byte-identical frames (the
    same file uploaded twice, a re-export) share a key. Computed from the image
    content, so distinct shots never falsely group. (Perceptual near-dup of burst
    frames is a vision-model enhancement; the mock floor is exact duplicates.)"""
    return "d_" + hashlib.sha256(data).hexdigest()[:16]

_KEYWORD_PALETTE = [
    "golden-hour", "candid", "portrait", "detail", "bokeh", "monochrome",
    "backlit", "wide-angle", "close-up", "ceremony", "reception", "natural-light",
    "documentary", "editorial", "macro", "silhouette", "motion", "symmetry",
]


@dataclass
class VisionResult:
    keywords: list[str] = field(default_factory=list)
    keeper_score: float = 0.0      # 0..1 — is this a technical keeper?
    hero_potential: float = 0.0    # 0..1 — could this be a hero/cover shot?
    shot_type: str = "candid"
    alt_text: str = ""
    eyes_closed: float = 0.0       # 0..1 — likelihood a subject blinked

    def as_dict(self) -> dict:
        return {
            "keywords": self.keywords,
            "keeper_score": round(self.keeper_score, 3),
            "hero_potential": round(self.hero_potential, 3),
            "shot_type": self.shot_type,
            "alt_text": self.alt_text,
            "eyes_closed": round(self.eyes_closed, 3),
        }


class VisionError(RuntimeError):
    pass


# ── Providers ───────────────────────────────────────────────────────────────


class MockVisionProvider:
    """Deterministic, network-free analysis derived from the filename."""

    backend = "mock"

    def analyze(self, *, filename: str, data: bytes, style: str = "") -> VisionResult:
        h = hashlib.sha256(filename.encode()).digest()
        kw = [_KEYWORD_PALETTE[h[i] % len(_KEYWORD_PALETTE)] for i in range(3)]
        # de-dup while preserving order
        keywords = list(dict.fromkeys(kw))
        keeper = 0.55 + (h[3] / 255) * 0.45            # 0.55..1.0
        hero = (h[4] / 255)                            # 0..1
        shot = SHOT_TYPES[h[5] % len(SHOT_TYPES)]
        eyes_closed = h[6] / 255                       # 0..1 — blink likelihood
        # A studio style profile re-weights the hero ranking deterministically
        # (the mock stand-in for "weight toward frames matching this look").
        if style.strip():
            sb = hashlib.sha256((style.strip() + "|" + filename).encode()).digest()[0] / 255
            hero = max(0.0, min(1.0, 0.5 * hero + 0.5 * sb))
        alt = f"{shot} photograph featuring {', '.join(keywords)}"
        return VisionResult(keywords=keywords, keeper_score=keeper, hero_potential=hero,
                            shot_type=shot, alt_text=alt, eyes_closed=eyes_closed)


class XaiVisionProvider:
    """xAI Grok vision backend. Best-effort JSON extraction with safe fallback."""

    backend = "xai"

    def __init__(self, settings: Settings):
        self.settings = settings

    def analyze(self, *, filename: str, data: bytes, style: str = "") -> VisionResult:
        import base64

        import httpx

        if not self.settings.xai_api_key:
            raise VisionError("HESTIA_XAI_API_KEY not set for xai vision backend")
        b64 = base64.b64encode(data).decode()
        style_line = (f" The studio's style preference is: {style.strip()}. Weight keeper_score "
                      "and hero_potential toward frames that match it." if style.strip() else "")
        prompt = (
            "You are a photo-culling assistant. Return ONLY compact JSON with keys: "
            "keywords (array of 3-6 lowercase strings), keeper_score (0-1 float), "
            "hero_potential (0-1 float), shot_type (one word), alt_text (one sentence), "
            "eyes_closed (0-1 float — likelihood a subject blinked or has closed eyes)."
            + style_line
        )
        body = {
            "model": self.settings.xai_model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
            "temperature": 0.2,
        }
        try:
            with httpx.Client(base_url=self.settings.xai_base_url, timeout=60) as c:
                resp = c.post("/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.settings.xai_api_key}"})
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            parsed = json.loads(_extract_json(content))
        except Exception as exc:  # noqa: BLE001 - degrade to a usable result
            raise VisionError(f"xai vision failed: {exc}") from exc
        return VisionResult(
            keywords=[str(k) for k in parsed.get("keywords", [])][:6],
            keeper_score=float(parsed.get("keeper_score", 0.0)),
            hero_potential=float(parsed.get("hero_potential", 0.0)),
            shot_type=str(parsed.get("shot_type", "candid")),
            alt_text=str(parsed.get("alt_text", "")),
            eyes_closed=float(parsed.get("eyes_closed", 0.0)),
        )


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else "{}"


def build_provider(settings: Settings):
    if settings.vision_backend == "xai":
        return XaiVisionProvider(settings)
    return MockVisionProvider()


# ── Gallery-level orchestration (in-process) ────────────────────────────────


def analyze_gallery(
    conn: sqlite3.Connection,
    storage: Storage,
    settings: Settings,
    *,
    tenant_id: str,
    gallery_id: int,
    hero_count: int = 5,
    provider=None,
) -> dict:
    """Analyze every image in a gallery, persist results, return a summary."""
    from .galleries import list_images

    provider = provider or build_provider(settings)
    images = list_images(conn, gallery_id)
    if not images:
        raise VisionError("gallery has no images to analyze")

    # The studio's AI style profile (Studio Pro) biases the keeper/hero scoring.
    srow = conn.execute("SELECT vision_style FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    style = (srow["vision_style"] if srow else "") or ""

    analyzed = []
    dup_keys: dict[int, str] = {}
    for img in images:
        data = storage.open(img["storage_key"])
        result = provider.analyze(filename=img["filename"], data=data, style=style)
        img_dup_key = content_dup_key(data)
        dup_keys[img["id"]] = img_dup_key
        conn.execute(
            """
            INSERT INTO image_analyses
                (image_id, gallery_id, tenant_id, keywords_json, keeper_score,
                 hero_potential, shot_type, alt_text, eyes_closed, dup_key)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (image_id) DO UPDATE SET
                keywords_json=excluded.keywords_json, keeper_score=excluded.keeper_score,
                hero_potential=excluded.hero_potential, shot_type=excluded.shot_type,
                alt_text=excluded.alt_text, eyes_closed=excluded.eyes_closed,
                dup_key=excluded.dup_key
            """,
            (img["id"], gallery_id, tenant_id, json.dumps(result.keywords),
             result.keeper_score, result.hero_potential, result.shot_type, result.alt_text,
             result.eyes_closed, img_dup_key),
        )
        analyzed.append((img, result))
    conn.commit()

    # Cull: in each duplicate cluster keep only the best keeper; flag likely blinks.
    # Heroes are then drawn only from the kept frames — never a dup or a blink.
    clusters: dict[str, list] = {}
    for img, r in analyzed:
        clusters.setdefault(dup_keys[img["id"]], []).append((img, r))
    duplicate_ids: set[int] = set()
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        best = max(cluster, key=lambda pair: pair[1].keeper_score)
        duplicate_ids.update(img["id"] for img, _ in cluster if img["id"] != best[0]["id"])
    blink_ids = {img["id"] for img, r in analyzed if r.eyes_closed >= BLINK_THRESHOLD}
    culled_ids = duplicate_ids | blink_ids

    kept = [(img, r) for img, r in analyzed if img["id"] not in culled_ids]
    ranked = sorted(kept, key=lambda pair: pair[1].hero_potential, reverse=True)
    hero_image_ids = [img["id"] for img, _ in ranked[:hero_count]]
    keeper_count = sum(1 for img, r in analyzed
                       if r.keeper_score >= KEEPER_THRESHOLD and img["id"] not in culled_ids)
    counts: dict[str, int] = {}
    for _, r in analyzed:
        for kw in r.keywords:
            counts[kw] = counts.get(kw, 0) + 1
    top_keywords = [kw for kw, _ in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:12]]

    return {
        "backend": getattr(provider, "backend", "mock"),
        "image_count": len(images),
        "analyzed": len(analyzed),
        "keeper_count": keeper_count,
        "hero_image_ids": hero_image_ids,
        "keywords": top_keywords,
        "duplicate_count": len(duplicate_ids),
        "blink_count": len(blink_ids),
        "culled_count": len(culled_ids),
        "culled_image_ids": sorted(culled_ids),
        "style_applied": bool(style.strip()),
    }


def cull_summary(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict:
    """Recompute the cull picture from persisted analyses, for the owner view —
    which frames are near-duplicates, likely blinks, or otherwise culled."""
    rows = conn.execute(
        "SELECT image_id, keeper_score, eyes_closed, dup_key FROM image_analyses "
        "WHERE tenant_id = ? AND gallery_id = ?",
        (tenant_id, gallery_id),
    ).fetchall()
    clusters: dict[str, list] = {}
    for r in rows:
        if r["dup_key"]:
            clusters.setdefault(r["dup_key"], []).append(r)
    duplicate_ids: set[int] = set()
    for cluster in clusters.values():
        if len(cluster) < 2:
            continue
        best = max(cluster, key=lambda x: x["keeper_score"] or 0)
        duplicate_ids.update(r["image_id"] for r in cluster if r["image_id"] != best["image_id"])
    blink_ids = {r["image_id"] for r in rows if (r["eyes_closed"] or 0) >= BLINK_THRESHOLD}
    return {
        "analyzed": len(rows),
        "duplicate_ids": duplicate_ids,
        "blink_ids": blink_ids,
        "culled_ids": duplicate_ids | blink_ids,
    }

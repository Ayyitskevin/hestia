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

    def as_dict(self) -> dict:
        return {
            "keywords": self.keywords,
            "keeper_score": round(self.keeper_score, 3),
            "hero_potential": round(self.hero_potential, 3),
            "shot_type": self.shot_type,
            "alt_text": self.alt_text,
        }


class VisionError(RuntimeError):
    pass


# ── Providers ───────────────────────────────────────────────────────────────


class MockVisionProvider:
    """Deterministic, network-free analysis derived from the filename."""

    backend = "mock"

    def analyze(self, *, filename: str, data: bytes) -> VisionResult:
        h = hashlib.sha256(filename.encode()).digest()
        kw = [_KEYWORD_PALETTE[h[i] % len(_KEYWORD_PALETTE)] for i in range(3)]
        # de-dup while preserving order
        keywords = list(dict.fromkeys(kw))
        keeper = 0.55 + (h[3] / 255) * 0.45            # 0.55..1.0
        hero = (h[4] / 255)                            # 0..1
        shot = SHOT_TYPES[h[5] % len(SHOT_TYPES)]
        alt = f"{shot} photograph featuring {', '.join(keywords)}"
        return VisionResult(keywords=keywords, keeper_score=keeper,
                            hero_potential=hero, shot_type=shot, alt_text=alt)


class XaiVisionProvider:
    """xAI Grok vision backend. Best-effort JSON extraction with safe fallback."""

    backend = "xai"

    def __init__(self, settings: Settings):
        self.settings = settings

    def analyze(self, *, filename: str, data: bytes) -> VisionResult:
        import base64

        import httpx

        if not self.settings.xai_api_key:
            raise VisionError("HESTIA_XAI_API_KEY not set for xai vision backend")
        b64 = base64.b64encode(data).decode()
        prompt = (
            "You are a photo-culling assistant. Return ONLY compact JSON with keys: "
            "keywords (array of 3-6 lowercase strings), keeper_score (0-1 float), "
            "hero_potential (0-1 float), shot_type (one word), alt_text (one sentence)."
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

    analyzed = []
    for img in images:
        data = storage.open(img["storage_key"])
        result = provider.analyze(filename=img["filename"], data=data)
        conn.execute(
            """
            INSERT INTO image_analyses
                (image_id, gallery_id, tenant_id, keywords_json, keeper_score,
                 hero_potential, shot_type, alt_text)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (image_id) DO UPDATE SET
                keywords_json=excluded.keywords_json, keeper_score=excluded.keeper_score,
                hero_potential=excluded.hero_potential, shot_type=excluded.shot_type,
                alt_text=excluded.alt_text
            """,
            (img["id"], gallery_id, tenant_id, json.dumps(result.keywords),
             result.keeper_score, result.hero_potential, result.shot_type, result.alt_text),
        )
        analyzed.append((img, result))
    conn.commit()

    # Heroes = top by hero_potential; keyword cloud = most common keywords.
    ranked = sorted(analyzed, key=lambda pair: pair[1].hero_potential, reverse=True)
    hero_image_ids = [img["id"] for img, _ in ranked[:hero_count]]
    keeper_count = sum(1 for _, r in analyzed if r.keeper_score >= 0.7)
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
    }

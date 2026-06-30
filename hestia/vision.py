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

# Per-frame technical sub-scores feed owner-facing advisory flags only (never auto-cull):
# exposure is overall brightness (0 dark .. 1 blown), sharpness is focus (0 soft .. 1 sharp).
SHARP_THRESHOLD = 0.40    # softer than this → "soft" (likely out of focus / motion-blurred)
DARK_THRESHOLD = 0.35     # darker than this → "dark" (underexposed)
BRIGHT_THRESHOLD = 0.90   # brighter than this → "bright" (overexposed / blown highlights)


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
    exposure: float = 0.5          # 0..1 — overall brightness (0 dark .. 1 blown), 0.5 well-exposed
    sharpness: float = 0.5         # 0..1 — focus (0 soft/blurred .. 1 tack-sharp)

    def as_dict(self) -> dict:
        return {
            "keywords": self.keywords,
            "keeper_score": round(self.keeper_score, 3),
            "hero_potential": round(self.hero_potential, 3),
            "shot_type": self.shot_type,
            "alt_text": self.alt_text,
            "eyes_closed": round(self.eyes_closed, 3),
            "exposure": round(self.exposure, 3),
            "sharpness": round(self.sharpness, 3),
        }


class VisionError(RuntimeError):
    pass


def _as_float(value, default: float = 0.0) -> float:
    """Coerce a model-supplied field to float, defaulting on junk. A live LLM may
    return a string ("high"), null, or omit the key — none of which should crash."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _score(value) -> float:
    """A 0..1 model score: tolerant of junk and clamped to range."""
    return min(1.0, max(0.0, _as_float(value, 0.0)))


def _score_or(value, default: float) -> float:
    """A 0..1 score that falls back to ``default`` (not 0) when the field is missing or junk —
    used for sub-scores where a missing value should read as neutral, not as "worst case"
    (e.g. an omitted exposure shouldn't flag the frame as underexposed)."""
    return min(1.0, max(0.0, _as_float(value, default)))


def _result_from_parsed(parsed: dict) -> VisionResult:
    """Build a VisionResult from a model's parsed JSON, tolerating imperfect output
    (a string/null where a number was asked for, a non-list ``keywords``). This is
    the seam between an unpredictable LLM and the rest of the pipeline — it must
    never raise on shape, or one frame's odd response strands the whole run in
    'running'."""
    raw_kw = parsed.get("keywords")
    return VisionResult(
        keywords=[str(k) for k in raw_kw][:6] if isinstance(raw_kw, list) else [],
        keeper_score=_score(parsed.get("keeper_score")),
        hero_potential=_score(parsed.get("hero_potential")),
        shot_type=str(parsed.get("shot_type") or "candid"),
        alt_text=str(parsed.get("alt_text") or ""),
        eyes_closed=_score(parsed.get("eyes_closed")),
        exposure=_score_or(parsed.get("exposure"), 0.5),
        sharpness=_score_or(parsed.get("sharpness"), 0.5),
    )


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
        # Technical sub-scores, biased toward "fine" so only a deterministic minority of
        # frames trip a flag (most photos are well-exposed and in focus).
        exposure = 0.30 + (h[7] / 255) * 0.65          # 0.30..0.95
        sharpness = 0.30 + (h[8] / 255) * 0.68         # 0.30..0.98
        # A studio style profile re-weights the hero ranking deterministically
        # (the mock stand-in for "weight toward frames matching this look").
        if style.strip():
            sb = hashlib.sha256((style.strip() + "|" + filename).encode()).digest()[0] / 255
            hero = max(0.0, min(1.0, 0.5 * hero + 0.5 * sb))
        alt = f"{shot} photograph featuring {', '.join(keywords)}"
        return VisionResult(keywords=keywords, keeper_score=keeper, hero_potential=hero,
                            shot_type=shot, alt_text=alt, eyes_closed=eyes_closed,
                            exposure=exposure, sharpness=sharpness)


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
            "eyes_closed (0-1 float — likelihood a subject blinked or has closed eyes), "
            "exposure (0-1 float — overall brightness: ~0 underexposed/dark, ~0.5 well-exposed, "
            "~1 overexposed/blown), sharpness (0-1 float — 1 tack-sharp/in focus, 0 soft/blurred)."
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
        # Coercion lives in _result_from_parsed so a junk field (string/null where a
        # number was asked for) degrades to a default instead of raising past the
        # pipeline's VisionError handler and stranding the run in 'running'.
        return _result_from_parsed(parsed)


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
    if not conn.execute(
        "SELECT 1 FROM galleries WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone():
        raise VisionError("gallery not found for tenant")
    images = list_images(conn, gallery_id, tenant_id=tenant_id)
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
                 hero_potential, shot_type, alt_text, eyes_closed, dup_key,
                 exposure, sharpness)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (image_id) DO UPDATE SET
                keywords_json=excluded.keywords_json, keeper_score=excluded.keeper_score,
                hero_potential=excluded.hero_potential, shot_type=excluded.shot_type,
                alt_text=excluded.alt_text, eyes_closed=excluded.eyes_closed,
                dup_key=excluded.dup_key, exposure=excluded.exposure,
                sharpness=excluded.sharpness
            """,
            (img["id"], gallery_id, tenant_id, json.dumps(result.keywords),
             result.keeper_score, result.hero_potential, result.shot_type, result.alt_text,
             result.eyes_closed, img_dup_key, result.exposure, result.sharpness),
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


def _norm_keyword(raw: str) -> str:
    """Normalize a keyword to its stored token form — lowercase, trimmed. The mock palette
    and the Grok prompt both emit lowercase tokens, so search matches case-insensitively."""
    return (raw or "").strip().lower()


def tenant_keyword_facets(conn: sqlite3.Connection, tenant_id: str, *, limit: int = 40) -> list[dict]:
    """The studio's keyword cloud across every analyzed frame: distinct keywords with how
    many frames carry each, most common first. This is the AI's understanding of the whole
    catalog surfaced as a browse entry point. Tenant-scoped."""
    rows = conn.execute(
        "SELECT keywords_json FROM image_analyses WHERE tenant_id = ?", (tenant_id,)
    ).fetchall()
    counts: dict[str, int] = {}
    for r in rows:
        try:
            kws = json.loads(r["keywords_json"])
        except (TypeError, ValueError):
            continue
        if not isinstance(kws, list):
            continue
        for kw in kws:
            k = _norm_keyword(str(kw))
            if k:
                counts[k] = counts.get(k, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"keyword": k, "count": n} for k, n in ranked[:limit]]


def search_images(conn: sqlite3.Connection, tenant_id: str, *, keyword: str = "",
                  shot_type: str = "", keepers_only: bool = False, clean_only: bool = False,
                  limit: int = 120) -> list[dict]:
    """Analyzed frames across all the studio's galleries, filtered by AI keyword and/or shot
    type and/or "keepers only" (strong keeper score) and/or "clean only" (no soft/dark/bright
    technical flag) — the studio's catalog, searchable by what's in frame and how good it is,
    an axis a Lightroom-export-to-gallery workflow can't offer. Tenant-scoped; needs at least
    one filter (returns [] otherwise). ``keepers_only`` also ranks by keeper score (best
    first); otherwise results are newest gallery first. Each row carries its gallery context,
    alt text, shot type."""
    kw = _norm_keyword(keyword)
    shot = _norm_keyword(shot_type)
    if not kw and not shot and not keepers_only and not clean_only:
        return []
    where = ["a.tenant_id = ?"]
    params: list = [tenant_id]
    if kw:
        # Match the JSON-quoted token (``"candid"``) so "close" can't match "close-up", and
        # escape LIKE metacharacters in the user input so % and _ are treated literally.
        safe = kw.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("a.keywords_json LIKE ? ESCAPE '\\'")
        params.append(f'%"{safe}"%')
    if shot:
        where.append("LOWER(a.shot_type) = ?")
        params.append(shot)
    if keepers_only:
        # NULL keeper_score is excluded by the >= comparison, which is what we want.
        where.append("a.keeper_score >= ?")
        params.append(KEEPER_THRESHOLD)
    if clean_only:
        # Exclude frames the sub-scores flag (soft / dark / bright). A NULL sub-score isn't
        # known to be a problem, so it counts as clean — matching _quality_flags.
        where.append("(a.sharpness IS NULL OR a.sharpness >= ?)")
        params.append(SHARP_THRESHOLD)
        where.append("(a.exposure IS NULL OR (a.exposure >= ? AND a.exposure <= ?))")
        params.extend([DARK_THRESHOLD, BRIGHT_THRESHOLD])
    order = ("a.keeper_score DESC, g.created_at DESC, i.position, i.id" if keepers_only
             else "g.created_at DESC, i.position, i.id")
    params.append(limit)
    rows = conn.execute(
        "SELECT i.id, i.filename, i.storage_key, i.gallery_id, i.hidden, "
        "       g.title AS gallery_title, g.slug AS gallery_slug, "
        "       a.alt_text, a.shot_type, a.keeper_score "
        "FROM image_analyses a "
        "JOIN images i ON i.id = a.image_id "
        "JOIN galleries g ON g.id = a.gallery_id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY " + order + " LIMIT ?",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def search_images_by_keyword(conn: sqlite3.Connection, tenant_id: str, keyword: str, *,
                             limit: int = 120) -> list[dict]:
    """Back-compat shim for keyword-only search. See :func:`search_images`."""
    return search_images(conn, tenant_id, keyword=keyword, limit=limit)


def tenant_shot_type_facets(conn: sqlite3.Connection, tenant_id: str, *,
                            limit: int = 12) -> list[dict]:
    """The studio's shot-type breakdown across analyzed frames (portrait, candid, detail,
    wide, …) with counts, most common first — a second browse axis for the Library."""
    rows = conn.execute(
        "SELECT LOWER(shot_type) AS shot_type, COUNT(*) AS n FROM image_analyses "
        "WHERE tenant_id = ? AND TRIM(COALESCE(shot_type, '')) <> '' "
        "GROUP BY LOWER(shot_type) ORDER BY n DESC, shot_type LIMIT ?",
        (tenant_id, limit),
    ).fetchall()
    return [{"shot_type": r["shot_type"], "count": r["n"]} for r in rows]


def alt_text_map(conn: sqlite3.Connection, gallery_id: int) -> dict[int, str]:
    """Per-image AI alt text for a gallery: ``{image_id: alt_text}`` for the frames the
    vision pass captioned. Used on the client gallery and delivery so every delivered photo
    carries a real, descriptive ``alt`` (accessibility + SEO) instead of a bare filename —
    callers fall back to the filename for any frame without a caption. Scoped by gallery_id,
    which the caller has already resolved for the tenant/token (like ``list_images``)."""
    rows = conn.execute(
        "SELECT image_id, alt_text FROM image_analyses WHERE gallery_id = ?", (gallery_id,)
    ).fetchall()
    return {r["image_id"]: r["alt_text"] for r in rows if (r["alt_text"] or "").strip()}


def _quality_flags(exposure, sharpness) -> list[str]:
    """Advisory technical flags from the sub-scores — surfaced to the owner, never auto-cull.
    A NULL score (frame analysed before these existed) yields no flag for that dimension."""
    flags = []
    if sharpness is not None and sharpness < SHARP_THRESHOLD:
        flags.append("soft")
    if exposure is not None:
        if exposure < DARK_THRESHOLD:
            flags.append("dark")
        elif exposure > BRIGHT_THRESHOLD:
            flags.append("bright")
    return flags


def gallery_analysis_map(conn: sqlite3.Connection, gallery_id: int) -> dict[int, dict]:
    """Per-image AI analysis for a gallery's owner view: ``{image_id: {keywords, shot_type,
    keeper_score, keeper, flags}}``. Lets the owner see what the AI saw on each frame — the
    tags link to the Library, and ``flags`` lists advisory technical issues (soft/dark/bright)
    from the exposure & sharpness sub-scores. Scoped by gallery_id, which the owner route has
    already resolved for the tenant."""
    rows = conn.execute(
        "SELECT image_id, keywords_json, shot_type, keeper_score, exposure, sharpness "
        "FROM image_analyses WHERE gallery_id = ?", (gallery_id,)
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        try:
            kws = json.loads(r["keywords_json"])
        except (TypeError, ValueError):
            kws = []
        if not isinstance(kws, list):
            kws = []
        score = r["keeper_score"]
        out[r["image_id"]] = {
            "keywords": [str(k) for k in kws][:6],
            "shot_type": r["shot_type"] or "",
            "keeper_score": score,
            "keeper": (score or 0) >= KEEPER_THRESHOLD,
            "flags": _quality_flags(r["exposure"], r["sharpness"]),
        }
    return out


def flagged_image_ids(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> set[int]:
    """Image ids in a gallery the vision pass flags as a likely technical reject (soft, dark
    or bright), from the exposure & sharpness sub-scores. Tenant-scoped. Advisory — the owner
    chooses whether to hide them (``apply_quality_cull``)."""
    rows = conn.execute(
        "SELECT image_id, exposure, sharpness FROM image_analyses "
        "WHERE tenant_id = ? AND gallery_id = ?",
        (tenant_id, gallery_id),
    ).fetchall()
    return {r["image_id"] for r in rows if _quality_flags(r["exposure"], r["sharpness"])}


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


def hero_suggestions(conn: sqlite3.Connection, tenant_id: str, gallery_id: int, *,
                     limit: int = 4) -> list[int]:
    """The AI's best cover candidates for a gallery: highest ``hero_potential`` among frames
    that aren't culled (near-dup/blink) or hidden, best first. Tenant-scoped. Lets the owner
    one-click set the strongest frame as the gallery cover."""
    culled = cull_summary(conn, tenant_id, gallery_id).get("culled_ids") or set()
    rows = conn.execute(
        "SELECT a.image_id FROM image_analyses a JOIN images i ON i.id = a.image_id "
        "WHERE a.tenant_id = ? AND a.gallery_id = ? AND i.hidden = 0 "
        "ORDER BY a.hero_potential DESC, a.image_id",
        (tenant_id, gallery_id),
    ).fetchall()
    return [r["image_id"] for r in rows if r["image_id"] not in culled][:limit]

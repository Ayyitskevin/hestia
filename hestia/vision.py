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
import io
import json
import math
import sqlite3
from dataclasses import dataclass, field

from .config import Settings
from .storage import Storage
from .xai import XaiTransport

SHOT_TYPES = ["portrait", "candid", "detail", "wide", "group", "landscape", "still-life"]

# A frame is flagged as a likely blink at/above this eyes-closed score, and a
# duplicate cluster keeps only its highest-keeper pick (the rest are culls).
BLINK_THRESHOLD = 0.85
KEEPER_THRESHOLD = 0.7

# Perceptual near-duplicate clustering. Two burst frames that look almost
# identical (same moment, fractional exposure/pose differences) rarely hash to
# the *same* 64-bit aHash, so we cluster by Hamming distance instead of exact
# equality: frames differing in ≤ this many bits are treated as one moment and
# culled to the best keeper. 5 bits on a 64-bit aHash catches a typical burst
# while keeping genuinely different compositions apart.
DUP_HAMMING_THRESHOLD = 5

# Per-frame technical sub-scores feed owner-facing advisory flags only (never auto-cull):
# exposure is overall brightness (0 dark .. 1 blown), sharpness is focus (0 soft .. 1 sharp).
SHARP_THRESHOLD = 0.40    # softer than this → "soft" (likely out of focus / motion-blurred)
DARK_THRESHOLD = 0.35     # darker than this → "dark" (underexposed)
BRIGHT_THRESHOLD = 0.90   # brighter than this → "bright" (overexposed / blown highlights)

# Bound every model-controlled text field before it reaches SQLite, templates,
# search facets, or logs. The provider prompt asks for 3-6 short tokens and one
# descriptive sentence; larger output is malformed, not additional product value.
_MAX_KEYWORDS = 6
_MAX_KEYWORD_CANDIDATES = 24
_MAX_KEYWORD_CHARS = 64
_MAX_ALT_TEXT_CHARS = 500
MAX_VISION_RESPONSE_BYTES = 64 * 1024


def content_dup_key(data: bytes) -> str:
    """A content signature for *exact* duplicate detection — byte-identical frames
    (the same file uploaded twice, a re-export) share a key. Computed from the
    image content, so distinct shots never falsely group. This is the mock floor
    used when Pillow isn't available; with Pillow, :func:`perceptual_hash` drives
    near-duplicate clustering instead (see :func:`cluster_duplicate_ids`)."""
    return "d_" + hashlib.sha256(data).hexdigest()[:16]


def perceptual_hash(data: bytes) -> int | None:
    """A 64-bit average hash (aHash) of a decoded image for near-duplicate detection.

    Resize to 8×8 grayscale, threshold each pixel against the mean → a 64-bit
    signature that's stable across minor exposure/JPEG/pose differences (a burst)
    but distinct across genuinely different compositions. Returns ``None`` when
    Pillow is unavailable or the bytes don't decode as an image, so the caller
    degrades to the exact-content floor (:func:`content_dup_key`) — never raises.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            gray = im.convert("L").resize((8, 8), Image.Resampling.LANCZOS)
            if hasattr(gray, "get_flattened_data"):
                pixels = list(gray.get_flattened_data())
            else:
                pixels = list(gray.getdata())  # Pillow < 12.1 compatibility
    except Exception:  # noqa: BLE001 - undecodable bytes are "not an image", not a crash
        return None
    avg = sum(pixels) / 64.0
    bits = 0
    for i, p in enumerate(pixels):
        if p >= avg:
            bits |= 1 << (63 - i)
    return bits


def _phash_hex(h: int) -> str:
    return "p_" + f"{h:016x}"


def _phash_from_hex(s: str) -> int | None:
    """Inverse of :func:`_phash_hex`; ``None`` for non-perceptual keys."""
    if not s or not s.startswith("p_"):
        return None
    try:
        return int(s[2:], 16)
    except ValueError:
        return None


def _hamming_distance(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _hamming_duplicate_ids(
    items: list[tuple[int, int, float]], threshold: int = DUP_HAMMING_THRESHOLD,
) -> set[int]:
    """Group ``items`` (image_id, phash, keeper_score) by Hamming distance ≤
    threshold (union-find), keep the best keeper in each cluster, return the rest
    as duplicate ids. O(n²) on the perceptual set, which is bounded by gallery
    size — fine for hundreds-to-low-thousands of frames."""
    n = len(items)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            if _hamming_distance(items[i][1], items[j][1]) <= threshold:
                union(i, j)
    clusters: dict[int, list[int]] = {}
    for i in range(n):
        clusters.setdefault(find(i), []).append(i)
    dup_ids: set[int] = set()
    for members in clusters.values():
        if len(members) < 2:
            continue
        best = max(members, key=lambda i: items[i][2])
        dup_ids.update(items[i][0] for i in members if i != best)
    return dup_ids


def cluster_duplicate_ids(
    rows, threshold: int = DUP_HAMMING_THRESHOLD,
) -> set[int]:
    """The shared near-duplicate cull: given ``(image_id, dup_key, keeper_score)``
    tuples, return the ids of the non-best frame in each duplicate cluster.

    A perceptual key (``p_<hex>``) clusters by Hamming distance ≤ threshold, so a
    burst of near-identical frames collapses to its single best keeper. A content
    key (``d_<sha>`` — the Pillow-absent floor) clusters by exact equality. Both
    the live run (:func:`analyze_gallery`) and the persisted recompute
    (:func:`cull_summary`) go through here, so the owner view matches what the
    pipeline culled."""
    perceptual: list[tuple[int, int, float]] = []
    exact: dict[str, list[tuple[int, float]]] = {}
    for image_id, dup_key, keeper in rows:
        ph = _phash_from_hex(dup_key)
        if ph is not None:
            perceptual.append((image_id, ph, keeper))
        elif dup_key:
            exact.setdefault(dup_key, []).append((image_id, keeper))
    dup_ids: set[int] = set()
    for group in exact.values():
        if len(group) < 2:
            continue
        best = max(group, key=lambda p: p[1])
        dup_ids.update(iid for iid, _ in group if iid != best[0])
    dup_ids |= _hamming_duplicate_ids(perceptual, threshold)
    return dup_ids

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


class VisionProviderError(VisionError):
    """A live-provider/configuration/result failure eligible for safe fallback."""


def _as_float(value, default: float = 0.0) -> float:
    """Coerce a model-supplied field to float, defaulting on junk. A live LLM may
    return a string ("high"), null, or omit the key — none of which should crash."""
    if isinstance(value, bool):
        return default
    if isinstance(value, str) and len(value) > 64:
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _score(value) -> float:
    """A 0..1 model score: tolerant of junk and clamped to range."""
    return min(1.0, max(0.0, _as_float(value, 0.0)))


def _score_or(value, default: float) -> float:
    """A 0..1 score that falls back to ``default`` (not 0) when the field is missing or junk —
    used for sub-scores where a missing value should read as neutral, not as "worst case"
    (e.g. an omitted exposure shouldn't flag the frame as underexposed)."""
    return min(1.0, max(0.0, _as_float(value, default)))


def _bounded_text(value, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    # Bound before splitting so pathological provider strings cannot force
    # unbounded temporary lists just to normalize whitespace.
    candidate = value[: max_chars * 2]
    candidate = "".join(
        "\ufffd" if 0xD800 <= ord(char) <= 0xDFFF else char for char in candidate
    )
    return " ".join(candidate.split())[:max_chars]


def _keywords(value) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for raw in value[:_MAX_KEYWORD_CANDIDATES]:
        keyword = _bounded_text(raw, max_chars=_MAX_KEYWORD_CHARS).lower()
        if keyword and keyword not in normalized:
            normalized.append(keyword)
        if len(normalized) == _MAX_KEYWORDS:
            break
    return normalized


def _result_from_parsed(parsed: dict) -> VisionResult:
    """Build a VisionResult from a model's parsed JSON, tolerating imperfect output
    (a string/null where a number was asked for, a non-list ``keywords``). This is
    the seam between an unpredictable LLM and the rest of the pipeline — it must
    never raise on shape, or one frame's odd response strands the whole run in
    'running'."""
    if not isinstance(parsed, dict):
        return VisionResult()
    shot_type = _bounded_text(parsed.get("shot_type"), max_chars=64).lower()
    if shot_type not in SHOT_TYPES:
        shot_type = "candid"
    return VisionResult(
        keywords=_keywords(parsed.get("keywords")),
        keeper_score=_score(parsed.get("keeper_score")),
        hero_potential=_score(parsed.get("hero_potential")),
        shot_type=shot_type,
        alt_text=_bounded_text(parsed.get("alt_text"), max_chars=_MAX_ALT_TEXT_CHARS),
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


def vision_prompt(style: str = "") -> str:
    """The xAI vision prompt — tuned for blink rejection, hero ranking, and technical flags."""
    style_line = ""
    if style.strip():
        style_line = (
            f" The studio's style preference is: {style.strip()}. "
            "Weight keeper_score and hero_potential toward frames that match it."
        )
    shot_types = ", ".join(SHOT_TYPES)
    return (
        "You are a professional photographer's culling assistant analyzing one frame. "
        "Return ONLY compact JSON with these keys:\n"
        "- keywords: array of 3-6 lowercase strings (subject, lighting, mood, moment)\n"
        f"- shot_type: exactly one of {shot_types}\n"
        "- keeper_score: 0-1 float — technical deliverability; penalize blur, bad exposure, "
        "awkward poses, and mid-blink expressions\n"
        "- hero_potential: 0-1 float — cover-worthy emotional peak; favor connection and "
        "story over mere sharpness; near-duplicates of the same moment score lower than "
        "the best frame\n"
        "- alt_text: one descriptive sentence for accessibility\n"
        "- eyes_closed: 0-1 float — likelihood ANY visible subject blinked or has eyes "
        "closed/mid-blink; score 0.85+ means likely discard\n"
        "- exposure: 0-1 float — ~0.3 underexposed, ~0.5 well-exposed, ~0.9+ blown highlights\n"
        "- sharpness: 0-1 float — 1 tack-sharp/in focus, below 0.4 soft or motion-blurred\n"
        "Score honestly. A technically sharp but emotionally flat frame should beat a blurry "
        "peak moment only when the blur ruins deliverability."
        + style_line
    )


class XaiVisionProvider:
    """xAI Grok vision backend. Best-effort JSON extraction with safe fallback."""

    backend = "xai"

    def __init__(self, settings: Settings, transport: XaiTransport | None = None):
        self.settings = settings
        self.transport = transport or XaiTransport(settings)

    def analyze(self, *, filename: str, data: bytes, style: str = "") -> VisionResult:
        import base64

        if not self.settings.xai_api_key:
            raise VisionProviderError("HESTIA_XAI_API_KEY not set for xai vision backend")
        b64 = base64.b64encode(data).decode()
        prompt = vision_prompt(style)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ]
        try:
            content = self.transport.chat_content(
                messages=messages,
                temperature=0.2,
                max_response_bytes=MAX_VISION_RESPONSE_BYTES,
            )
            parsed = json.loads(_extract_json(content))
            if not isinstance(parsed, dict):
                raise ValueError("xai vision response must be a JSON object")
            return _result_from_parsed(parsed)
        except Exception as exc:  # noqa: BLE001 - degrade to a usable result
            raise VisionProviderError(f"xai vision failed: {exc}") from exc


def _extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start >= 0 and end > start else text.strip()


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

    # The studio's AI style profile biases the keeper/hero scoring.
    srow = conn.execute("SELECT vision_style FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    style = (srow["vision_style"] if srow else "") or ""

    analyzed = []
    dup_keys: dict[int, str] = {}
    for img in images:
        data = storage.open(img["storage_key"])
        result = provider.analyze(filename=img["filename"], data=data, style=style)
        ph = perceptual_hash(data)
        img_dup_key = _phash_hex(ph) if ph is not None else content_dup_key(data)
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

    # Cull: near-duplicate clusters (perceptual Hamming, or exact-content floor)
    # keep only the best keeper; flag likely blinks. Heroes are then drawn only
    # from the kept frames — never a dup or a blink.
    dup_rows = [(img["id"], dup_keys[img["id"]], r.keeper_score) for img, r in analyzed]
    duplicate_ids = cluster_duplicate_ids(dup_rows)
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

    backend = getattr(provider, "backend", "mock")
    if backend != "mock":
        from .ai_usage import record_usage
        record_usage(conn, tenant_id=tenant_id, module="vision", backend=backend,
                     units=len(analyzed), gallery_id=gallery_id)
        conn.commit()

    return {
        "backend": backend,
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
        # Tenant-matched joins: the WHERE already filters a.tenant_id and image/gallery
        # ids are globally-unique PKs, so this is defense-in-depth — it makes the
        # single-tenant invariant explicit and can't return another studio's row even
        # if an analysis were ever mis-written against a foreign image/gallery id.
        "JOIN images i ON i.id = a.image_id AND i.tenant_id = a.tenant_id "
        "JOIN galleries g ON g.id = a.gallery_id AND g.tenant_id = a.tenant_id "
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
    which frames are near-duplicates, likely blinks, or otherwise culled. Uses
    the same :func:`cluster_duplicate_ids` as the live run so the owner view
    matches what the pipeline culled."""
    rows = conn.execute(
        "SELECT image_id, keeper_score, eyes_closed, dup_key FROM image_analyses "
        "WHERE tenant_id = ? AND gallery_id = ?",
        (tenant_id, gallery_id),
    ).fetchall()
    duplicate_ids = cluster_duplicate_ids(
        [(r["image_id"], r["dup_key"] or "", r["keeper_score"] or 0) for r in rows]
    )
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


VISION_CALIBRATION_COLUMNS = (
    "gallery_id",
    "gallery_title",
    "vision_backend",
    "fallback_from",
    "fallback_scope",
    "style_applied",
    "vision_completed_at",
    "image_id",
    "position",
    "filename",
    "content_type",
    "width",
    "height",
    "bytes",
    "analysis_status",
    "keywords",
    "shot_type",
    "alt_text",
    "keeper_score",
    "keeper_decision_at_0_70",
    "hero_potential",
    "pipeline_hero",
    "eyes_closed",
    "blink_flag_at_0_85",
    "duplicate_flag",
    "exposure",
    "sharpness",
    "quality_flags",
    "cull_apply_action",
    "hidden_current",
    "cover_current",
    "client_favorite_current",
    "review_keep",
    "review_reason",
    "review_notes",
)


def _yes_no(value: bool | None) -> str:
    if value is None:
        return ""
    return "yes" if value else "no"


def _strict_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    return None


def _optional_score(value) -> float | None:
    score = _as_float(value, math.nan)
    if not math.isfinite(score) or not 0.0 <= score <= 1.0:
        return None
    return score


def _canonical_dup_key(value) -> str | None:
    if not isinstance(value, str) or len(value) != 18 or value[:2] not in {"d_", "p_"}:
        return None
    try:
        int(value[2:], 16)
    except ValueError:
        return None
    return value


def _stored_shot_type(value) -> str:
    shot_type = _bounded_text(value, max_chars=64).lower()
    return shot_type if shot_type in SHOT_TYPES else ""


def _stored_vision_state(
    conn: sqlite3.Connection,
    tenant_id: str,
    gallery_id: int,
) -> tuple[dict, str]:
    row = conn.execute(
        "SELECT substr(steps_json, 1, 131072) AS steps_json FROM pipeline_runs "
        "WHERE tenant_id = ? AND source = 'gallery' AND source_id = ?",
        (tenant_id, str(gallery_id)),
    ).fetchone()
    if not row:
        return {}, ""
    try:
        steps = json.loads(row["steps_json"] or "[]")
    except (TypeError, ValueError):
        return {}, ""
    if not isinstance(steps, list):
        return {}, ""
    for step in steps:
        if not isinstance(step, dict) or step.get("name") != "vision":
            continue
        if step.get("status") != "done":
            return {}, ""
        output = step.get("output")
        summary = output.get("summary") if isinstance(output, dict) else None
        finished_at = _bounded_text(step.get("finished_at"), max_chars=64)
        return (summary if isinstance(summary, dict) else {}), finished_at
    return {}, ""


def gallery_calibration_rows(
    conn: sqlite3.Connection,
    tenant_id: str,
    gallery_id: int,
) -> list[dict]:
    """One latest-snapshot review row per frame, including scores and weak labels.

    The export is a calibration aid, not a new source of truth: blank review columns let
    the studio label each row offline. Current hidden/cover/favorite fields are context,
    not independent historical ground truth. The explicit tenant predicate and matched
    joins keep the read safe even if inconsistent foreign rows reach the database.
    """
    gallery = conn.execute(
        "SELECT id, substr(title, 1, 200) AS title, cover_image_id FROM galleries "
        "WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    if not gallery:
        return []

    cover_id = gallery["cover_image_id"]
    cover_known = cover_id is None or (
        isinstance(cover_id, int) and not isinstance(cover_id, bool)
    )
    summary, vision_completed_at = _stored_vision_state(conn, tenant_id, gallery_id)
    backend = _bounded_text(summary.get("backend"), max_chars=64)
    fallback_from = _bounded_text(summary.get("fallback_from"), max_chars=64)
    fallback_scope = _bounded_text(summary.get("fallback_scope"), max_chars=64)
    style_value = summary.get("style_applied")
    style_applied = _yes_no(style_value if isinstance(style_value, bool) else None)
    raw_hero_ids = summary.get("hero_image_ids")
    hero_ids_known = (
        isinstance(raw_hero_ids, list)
        and len(raw_hero_ids) <= 1000
        and all(
            isinstance(value, int) and not isinstance(value, bool) and value > 0
            for value in raw_hero_ids
        )
    )
    hero_ids = set(raw_hero_ids) if hero_ids_known else set()
    records = conn.execute(
        "SELECT i.id AS image_id, i.position, substr(i.filename, 1, 1024) AS filename, "
        "substr(i.content_type, 1, 128) AS content_type, i.width, "
        "i.height, i.bytes, i.hidden, a.id AS analysis_id, "
        "substr(a.keywords_json, 1, 4096) AS keywords_json, "
        "a.keeper_score, a.hero_potential, substr(a.shot_type, 1, 128) AS shot_type, "
        "substr(a.alt_text, 1, 1000) AS alt_text, a.eyes_closed, "
        "substr(a.dup_key, 1, 128) AS dup_key, a.exposure, a.sharpness, "
        "f.id IS NOT NULL AS client_favorite "
        "FROM images i LEFT JOIN image_analyses a ON a.image_id = i.id "
        "AND a.gallery_id = i.gallery_id AND a.tenant_id = i.tenant_id "
        "LEFT JOIN image_favorites f ON f.image_id = i.id AND f.gallery_id = i.gallery_id "
        "AND f.tenant_id = i.tenant_id "
        "WHERE i.tenant_id = ? AND i.gallery_id = ? ORDER BY i.position, i.id",
        (tenant_id, gallery_id),
    ).fetchall()

    normalized: dict[int, dict] = {}
    duplicate_inputs: list[tuple[int, str, float]] = []
    duplicate_evidence_complete = True
    for record in records:
        if record["analysis_id"] is None:
            continue
        keeper_score = _optional_score(record["keeper_score"])
        values = {
            "keeper_score": keeper_score,
            "hero_potential": _optional_score(record["hero_potential"]),
            "eyes_closed": _optional_score(record["eyes_closed"]),
            "exposure": _optional_score(record["exposure"]),
            "sharpness": _optional_score(record["sharpness"]),
            "dup_key": _canonical_dup_key(record["dup_key"]),
        }
        normalized[record["image_id"]] = values
        if values["dup_key"] is not None:
            if keeper_score is None:
                duplicate_evidence_complete = False
            else:
                duplicate_inputs.append(
                    (record["image_id"], values["dup_key"], keeper_score)
                )
    duplicate_ids = (
        cluster_duplicate_ids(duplicate_inputs) if duplicate_evidence_complete else set()
    )

    rows: list[dict] = []
    for record in records:
        analyzed = record["analysis_id"] is not None
        values = normalized.get(record["image_id"], {})
        try:
            raw_keywords = (
                json.loads((record["keywords_json"] or "")[:4096]) if analyzed else []
            )
        except (TypeError, ValueError):
            raw_keywords = []
        keywords = _keywords(raw_keywords)
        quality_flags = (
            _quality_flags(values.get("exposure"), values.get("sharpness"))
            if analyzed
            else []
        )
        keeper_score = values.get("keeper_score")
        hero_potential = values.get("hero_potential")
        eyes_closed = values.get("eyes_closed")
        duplicate = (
            record["image_id"] in duplicate_ids
            if analyzed
            and values.get("dup_key") is not None
            and duplicate_evidence_complete
            else None
        )
        blink = eyes_closed >= BLINK_THRESHOLD if eyes_closed is not None else None
        if duplicate is True or blink is True:
            cull_action = "hide"
        elif duplicate is False and blink is False:
            cull_action = "no_change"
        else:
            cull_action = ""
        rows.append(
            {
                "gallery_id": gallery["id"],
                "gallery_title": gallery["title"],
                "vision_backend": backend,
                "fallback_from": fallback_from,
                "fallback_scope": fallback_scope,
                "style_applied": style_applied,
                "vision_completed_at": vision_completed_at,
                "image_id": record["image_id"],
                "position": record["position"],
                "filename": record["filename"],
                "content_type": record["content_type"],
                "width": record["width"] if record["width"] is not None else "",
                "height": record["height"] if record["height"] is not None else "",
                "bytes": record["bytes"] if record["bytes"] is not None else "",
                "analysis_status": "analyzed" if analyzed else "not_analyzed",
                "keywords": "|".join(keywords),
                "shot_type": (
                    _stored_shot_type(record["shot_type"]) if analyzed else ""
                ),
                "alt_text": (
                    _bounded_text(record["alt_text"], max_chars=_MAX_ALT_TEXT_CHARS)
                    if analyzed
                    else ""
                ),
                "keeper_score": keeper_score if keeper_score is not None else "",
                "keeper_decision_at_0_70": _yes_no(
                    keeper_score >= KEEPER_THRESHOLD if keeper_score is not None else None
                ),
                "hero_potential": hero_potential if hero_potential is not None else "",
                "pipeline_hero": _yes_no(
                    record["image_id"] in hero_ids if hero_ids_known and analyzed else None
                ),
                "eyes_closed": eyes_closed if eyes_closed is not None else "",
                "blink_flag_at_0_85": _yes_no(blink),
                "duplicate_flag": _yes_no(duplicate),
                "exposure": values.get("exposure") if values.get("exposure") is not None else "",
                "sharpness": values.get("sharpness") if values.get("sharpness") is not None else "",
                "quality_flags": "|".join(quality_flags),
                "cull_apply_action": cull_action,
                "hidden_current": _yes_no(_strict_bool(record["hidden"])),
                "cover_current": _yes_no(
                    record["image_id"] == cover_id if cover_known else None
                ),
                "client_favorite_current": _yes_no(bool(record["client_favorite"])),
                "review_keep": "",
                "review_reason": "",
                "review_notes": "",
            }
        )
    return rows

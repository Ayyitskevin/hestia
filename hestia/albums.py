"""Album designer — drafted spreads from a gallery (essence of Mnemosyne, in-app).

Signature move, straight from Mnemosyne's CLAUDE.md: **the model proposes, the
code validates.** An arranger proposes an order for the gallery's frames; the code
then chunks them into spreads and *guarantees* every photo is placed exactly once
(no dropped or duplicated frames) and picks each spread's hero by vision score.

Pluggable arranger, same shape as the vision/payments seams:
- ``mock`` — deterministic order (gallery order; heroes surface per spread).
- ``xai`` — an LLM proposes the order; on any hiccup we fall back to deterministic.

Idempotent: one album per gallery, regenerated in place.
"""

from __future__ import annotations

import json
import sqlite3

from .config import Settings

PHOTOS_PER_SPREAD = 4


class AlbumError(RuntimeError):
    pass


# ── Arrangers (propose an order) ────────────────────────────────────────────


class MockArranger:
    backend = "mock"

    def propose(self, images: list[dict]) -> list[int]:
        # Deterministic: keep gallery order. Code validates + assigns heroes.
        return [img["id"] for img in images]


class XaiArranger:
    backend = "xai"

    def __init__(self, settings: Settings):
        self.settings = settings

    def propose(self, images: list[dict]) -> list[int]:
        order = [img["id"] for img in images]
        if not self.settings.xai_api_key:
            return order
        import httpx

        manifest = [{"id": i["id"], "shot_type": i.get("shot_type"),
                     "hero": round(i.get("hero_potential") or 0, 2)} for i in images]
        prompt = (
            "Order these wedding/event photos into a story for an album. Return ONLY "
            "a JSON array of the photo ids in your recommended order, every id exactly "
            f"once. Photos: {json.dumps(manifest)}"
        )
        try:
            with httpx.Client(base_url=self.settings.xai_base_url, timeout=60) as c:
                resp = c.post("/chat/completions", json={
                    "model": self.settings.xai_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.3,
                }, headers={"Authorization": f"Bearer {self.settings.xai_api_key}"})
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            proposed = json.loads(text[text.find("["): text.rfind("]") + 1])
            return [int(x) for x in proposed]
        except Exception:  # noqa: BLE001 - validate_and_repair fixes any mess
            return order


def build_arranger(settings: Settings):
    if settings.album_backend == "xai":
        return XaiArranger(settings)
    return MockArranger()


# ── The "code validates" guarantee ──────────────────────────────────────────


def validate_and_repair(proposed: list[int], all_ids: list[int]) -> list[int]:
    """Return a permutation of ``all_ids`` honoring ``proposed`` where valid.

    Drops ids not in the gallery and duplicates; appends any photo the proposal
    missed (in gallery order). Guarantees every photo appears exactly once.
    """
    allowed = set(all_ids)
    seen: set[int] = set()
    out: list[int] = []
    for pid in proposed:
        if pid in allowed and pid not in seen:
            out.append(pid)
            seen.add(pid)
    for pid in all_ids:  # backfill anything the proposer dropped
        if pid not in seen:
            out.append(pid)
            seen.add(pid)
    return out


def _build_spreads(ordered_ids: list[int], hero_by_id: dict[int, float],
                   per_spread: int) -> list[dict]:
    spreads = []
    for i in range(0, len(ordered_ids), per_spread):
        chunk = ordered_ids[i: i + per_spread]
        hero = max(chunk, key=lambda pid: hero_by_id.get(pid, 0.0))
        spreads.append({"position": len(spreads) + 1, "hero_image_id": hero, "photo_ids": chunk})
    return spreads


# ── Album generation (idempotent) ───────────────────────────────────────────


def generate_album(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    tenant: dict,
    gallery: dict,
    arranger=None,
    per_spread: int = PHOTOS_PER_SPREAD,
) -> dict:
    from .galleries import list_images

    images = list_images(conn, gallery["id"])
    if not images:
        raise AlbumError("gallery has no images to arrange")

    # Pull vision scores (may be absent if the gallery wasn't processed yet).
    rows = conn.execute(
        "SELECT image_id, hero_potential, shot_type FROM image_analyses WHERE gallery_id = ?",
        (gallery["id"],),
    ).fetchall()
    scores = {r["image_id"]: dict(r) for r in rows}
    enriched = []
    for img in images:
        sc = scores.get(img["id"]) or {}
        enriched.append({"id": img["id"], "position": img["position"],
                         "hero_potential": sc.get("hero_potential"),
                         "shot_type": sc.get("shot_type")})
    hero_by_id = {e["id"]: (e["hero_potential"] or 0.0) for e in enriched}
    all_ids = [img["id"] for img in images]

    arranger = arranger or build_arranger(settings)
    ordered = validate_and_repair(arranger.propose(enriched), all_ids)
    spreads = _build_spreads(ordered, hero_by_id, per_spread)
    title = f"{gallery['title']} — album"

    existing = conn.execute(
        "SELECT id FROM albums WHERE tenant_id = ? AND gallery_id = ?",
        (tenant["id"], gallery["id"]),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE albums SET title = ?, backend = ?, spreads_json = ?, updated_at = datetime('now') WHERE id = ?",
            (title, getattr(arranger, "backend", "mock"), json.dumps(spreads), existing["id"]),
        )
        album_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO albums (tenant_id, gallery_id, title, backend, spreads_json) VALUES (?, ?, ?, ?, ?)",
            (tenant["id"], gallery["id"], title, getattr(arranger, "backend", "mock"), json.dumps(spreads)),
        )
        album_id = cur.lastrowid
    conn.commit()
    return get_album(conn, tenant["id"], album_id)


def _hydrate(row: dict) -> dict:
    row["spreads"] = json.loads(row.pop("spreads_json") or "[]")
    row["photo_count"] = sum(len(s["photo_ids"]) for s in row["spreads"])
    return row


def get_album(conn: sqlite3.Connection, tenant_id: str, album_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM albums WHERE id = ? AND tenant_id = ?", (album_id, tenant_id)
    ).fetchone()
    return _hydrate(dict(row)) if row else None


def get_album_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM albums WHERE tenant_id = ? AND gallery_id = ?", (tenant_id, gallery_id)
    ).fetchone()
    return _hydrate(dict(row)) if row else None

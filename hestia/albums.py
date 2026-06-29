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
from .crypto import new_session_token

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

    existing = conn.execute(
        "SELECT id, approved_at FROM albums WHERE tenant_id = ? AND gallery_id = ?",
        (tenant["id"], gallery["id"]),
    ).fetchone()
    if existing and existing["approved_at"]:
        # The client has approved this layout — lock it. Re-arranging would change the album
        # out from under them, so return the approved album unchanged.
        return get_album(conn, tenant["id"], existing["id"])

    # Exclude culled/hidden frames — the album is a client deliverable, so it should never
    # arrange a frame the owner removed from the gallery (and the client review serves these).
    images = list_images(conn, gallery["id"], include_hidden=False)
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


# ── Client review + approval (unguessable link, same model as delivery/offers) ──


def enable_album_review(conn: sqlite3.Connection, tenant_id: str, album_id: int) -> str | None:
    """Ensure the album has a review token, minting one if absent. Idempotent and race-safe:
    the mint only writes when the token is still empty, so two concurrent 'share' requests
    can't strand the first link — the loser reads back the winner's token."""
    row = conn.execute(
        "SELECT review_token FROM albums WHERE id = ? AND tenant_id = ?", (album_id, tenant_id)
    ).fetchone()
    if not row:
        return None
    if row["review_token"]:
        return row["review_token"]
    token = new_session_token()
    cur = conn.execute(
        "UPDATE albums SET review_token = ? WHERE id = ? AND tenant_id = ? "
        "AND (review_token IS NULL OR review_token = '')",
        (token, album_id, tenant_id),
    )
    if cur.rowcount:
        return token
    fresh = conn.execute(
        "SELECT review_token FROM albums WHERE id = ? AND tenant_id = ?", (album_id, tenant_id)
    ).fetchone()
    return fresh["review_token"] if fresh else None


def get_album_by_review_token(conn: sqlite3.Connection, token: str) -> dict | None:
    if not token:
        return None
    row = conn.execute("SELECT * FROM albums WHERE review_token = ?", (token,)).fetchone()
    return _hydrate(dict(row)) if row else None


def approve_album(conn: sqlite3.Connection, token: str) -> bool:
    """Client signs off on the album — a one-way 'these spreads are good' signal. Claim-before
    -act: the guarded UPDATE only matches a not-yet-approved album, so just the FIRST approval
    wins (rowcount == 1); a double-submit or a re-opened link is a no-op that returns False and
    never re-stamps ``approved_at``."""
    if not token:
        return False
    cur = conn.execute(
        "UPDATE albums SET approved_at = datetime('now'), updated_at = datetime('now'), "
        "change_request = NULL, change_requested_at = NULL "
        "WHERE review_token = ? AND approved_at IS NULL",
        (token,),
    )
    if cur.rowcount != 1:
        return False
    row = conn.execute(
        "SELECT tenant_id, gallery_id, title FROM albums WHERE review_token = ?", (token,)
    ).fetchone()
    if row:
        from .automations import emit_event
        emit_event(conn, tenant_id=row["tenant_id"], event="album.approved",
                   context={"gallery_id": row["gallery_id"], "title": row["title"]})
    return True


def request_album_changes(conn: sqlite3.Connection, token: str, note: str) -> bool:
    """Client asks for changes instead of approving — records their note and notifies the
    owner (the ``album.changes_requested`` automation). Returns False on an empty note or an
    already-approved album (the review page hides the form once approved). The latest note
    wins; the album stays editable."""
    text = (note or "").strip()
    if not token or not text:
        return False
    cur = conn.execute(
        "UPDATE albums SET change_request = ?, change_requested_at = datetime('now'), "
        "updated_at = datetime('now') WHERE review_token = ? AND approved_at IS NULL",
        (text, token),
    )
    if cur.rowcount != 1:
        return False
    row = conn.execute(
        "SELECT tenant_id, gallery_id, title FROM albums WHERE review_token = ?", (token,)
    ).fetchone()
    if row:
        from .automations import emit_event
        emit_event(conn, tenant_id=row["tenant_id"], event="album.changes_requested",
                   context={"gallery_id": row["gallery_id"], "title": row["title"]})
    return True


def album_review_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/a/{token}"


def set_spread_hero(conn: sqlite3.Connection, tenant_id: str, album_id: int, position: int,
                    image_id: int) -> bool:
    """Override the AI's hero pick for one spread — the photographer chooses which frame leads
    it. Returns False if the album isn't this tenant's or the image isn't in that spread.
    Tenant-scoped; persists the edited spreads in place."""
    album = get_album(conn, tenant_id, album_id)
    if not album:
        return False
    if album.get("approved_at"):
        return False        # locked: the client approved this layout, don't edit it
    changed = False
    for sp in album["spreads"]:
        if sp["position"] == position and image_id in sp["photo_ids"]:
            sp["hero_image_id"] = image_id
            changed = True
            break
    if not changed:
        return False
    conn.execute(
        "UPDATE albums SET spreads_json = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (json.dumps(album["spreads"]), album_id, tenant_id),
    )
    return True


def move_spread(conn: sqlite3.Connection, tenant_id: str, album_id: int, position: int,
                direction: str) -> bool:
    """Reorder a spread one step ``up`` or ``down`` — the photographer's sequencing over the
    arranged order. Swaps with the neighbour and renumbers positions 1..N. Returns False at a
    boundary, on a bad position/direction, on an approved (locked) album, or another tenant's.
    Tenant-scoped; persists the reordered spreads in place."""
    if direction not in ("up", "down"):
        return False
    album = get_album(conn, tenant_id, album_id)
    if not album or album.get("approved_at"):
        return False
    spreads = album["spreads"]
    idx = next((i for i, sp in enumerate(spreads) if sp["position"] == position), None)
    if idx is None:
        return False
    swap = idx - 1 if direction == "up" else idx + 1
    if not 0 <= swap < len(spreads):
        return False        # already first/last — nothing to do
    spreads[idx], spreads[swap] = spreads[swap], spreads[idx]
    for i, sp in enumerate(spreads):
        sp["position"] = i + 1
    conn.execute(
        "UPDATE albums SET spreads_json = ?, updated_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (json.dumps(spreads), album_id, tenant_id),
    )
    return True


def album_spreads_display(conn: sqlite3.Connection, album: dict, url_builder) -> list[dict]:
    """Resolve an album's spreads into display dicts — ``[{position, photos: [{id, url,
    filename, is_hero}]}]``. ``url_builder(img)`` yields each photo's URL, so the owner view
    can use the storage path while the client review serves images through its review token."""
    from .galleries import get_image

    out = []
    for sp in album["spreads"]:
        photos = []
        for iid in sp["photo_ids"]:
            img = get_image(conn, album["tenant_id"], iid)
            # Skip a frame culled (hidden) after the album was generated — it stays in
            # spreads_json but must not render, matching the photo route's hidden=0 gate.
            if img and not img["hidden"]:
                photos.append({"id": iid, "url": url_builder(img),
                               "filename": img["filename"], "is_hero": iid == sp["hero_image_id"]})
        out.append({"position": sp["position"], "photos": photos})
    return out

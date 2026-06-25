"""Gallery proofing — client favorites and comments on delivered galleries.

Favorites are per gallery (one couple, one album), so a heart toggles idempotently
on the ``(gallery_id, image_id)`` unique key. Everything is tenant-scoped, and
writes validate that the image actually belongs to the gallery so a public caller
can't favorite or comment on a frame outside the link they were given.
"""

from __future__ import annotations

import sqlite3


def image_in_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int, image_id: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM images WHERE id = ? AND gallery_id = ? AND tenant_id = ?",
        (image_id, gallery_id, tenant_id),
    ).fetchone()
    return row is not None


def toggle_favorite(
    conn: sqlite3.Connection, *, tenant_id: str, gallery_id: int, image_id: int
) -> bool | None:
    """Toggle a favorite. Returns True if now favorited, False if removed, or None
    if the image isn't part of this gallery (nothing happens)."""
    if not image_in_gallery(conn, tenant_id, gallery_id, image_id):
        return None
    existing = conn.execute(
        "SELECT id FROM image_favorites WHERE gallery_id = ? AND image_id = ?",
        (gallery_id, image_id),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM image_favorites WHERE id = ?", (existing["id"],))
        return False
    conn.execute(
        "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
        (tenant_id, gallery_id, image_id),
    )
    return True


def favorite_image_ids(conn: sqlite3.Connection, gallery_id: int) -> set[int]:
    rows = conn.execute(
        "SELECT image_id FROM image_favorites WHERE gallery_id = ?", (gallery_id,)
    ).fetchall()
    return {r["image_id"] for r in rows}


def favorite_count(conn: sqlite3.Connection, gallery_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM image_favorites WHERE gallery_id = ?", (gallery_id,)
    ).fetchone()
    return row["n"] if row else 0


def list_favorites(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> list[dict]:
    """The favorited images (joined), for the owner's proofing view."""
    rows = conn.execute(
        "SELECT i.* FROM image_favorites f JOIN images i ON i.id = f.image_id "
        "WHERE f.gallery_id = ? AND f.tenant_id = ? ORDER BY i.position, i.id",
        (gallery_id, tenant_id),
    ).fetchall()
    return [dict(r) for r in rows]


def add_comment(
    conn: sqlite3.Connection, *, tenant_id: str, gallery_id: int, image_id: int,
    body: str, author_name: str = "",
) -> dict | None:
    """Add a client comment to an image. Returns None for an empty body or an
    image that isn't part of this gallery."""
    text = (body or "").strip()
    if not text or not image_in_gallery(conn, tenant_id, gallery_id, image_id):
        return None
    cur = conn.execute(
        "INSERT INTO image_comments (tenant_id, gallery_id, image_id, author_name, body) "
        "VALUES (?, ?, ?, ?, ?)",
        (tenant_id, gallery_id, image_id, author_name.strip()[:80], text[:1000]),
    )
    row = conn.execute("SELECT * FROM image_comments WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def comments_by_image(conn: sqlite3.Connection, gallery_id: int) -> dict[int, list[dict]]:
    """Map image_id → its comments (oldest first), for the client gallery view."""
    rows = conn.execute(
        "SELECT * FROM image_comments WHERE gallery_id = ? ORDER BY id", (gallery_id,)
    ).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["image_id"], []).append(dict(r))
    return out


def comments_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> list[dict]:
    """All comments (newest first, with the frame filename), for the owner view."""
    rows = conn.execute(
        "SELECT c.*, i.filename FROM image_comments c JOIN images i ON i.id = c.image_id "
        "WHERE c.gallery_id = ? AND c.tenant_id = ? ORDER BY c.id DESC",
        (gallery_id, tenant_id),
    ).fetchall()
    return [dict(r) for r in rows]

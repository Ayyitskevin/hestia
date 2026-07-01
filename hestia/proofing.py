"""Gallery proofing — client favorites and comments on delivered galleries.

Favorites are per gallery (one couple, one album), so a heart toggles idempotently
on the ``(gallery_id, image_id)`` unique key. Everything is tenant-scoped, and
writes validate that the image actually belongs to the gallery so a public caller
can't favorite or comment on a frame outside the link they were given.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict


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
        "SELECT id FROM image_favorites WHERE gallery_id = ? AND image_id = ? AND tenant_id = ?",
        (gallery_id, image_id, tenant_id),
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM image_favorites WHERE id = ?", (existing["id"],))
        return False
    conn.execute(
        "INSERT INTO image_favorites (tenant_id, gallery_id, image_id) VALUES (?, ?, ?)",
        (tenant_id, gallery_id, image_id),
    )
    return True


def favorite_image_ids(
    conn: sqlite3.Connection,
    gallery_id: int,
    *,
    tenant_id: str | None = None,
) -> set[int]:
    sql = "SELECT image_id FROM image_favorites WHERE gallery_id = ?"
    params: list = [gallery_id]
    if tenant_id is not None:
        sql += " AND tenant_id = ?"
        params.append(tenant_id)
    rows = conn.execute(sql, params).fetchall()
    return {r["image_id"] for r in rows}


def favorite_count(
    conn: sqlite3.Connection,
    gallery_id: int,
    *,
    tenant_id: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) AS n FROM image_favorites WHERE gallery_id = ?"
    params: list = [gallery_id]
    if tenant_id is not None:
        sql += " AND tenant_id = ?"
        params.append(tenant_id)
    row = conn.execute(sql, params).fetchone()
    return row["n"] if row else 0


def list_favorites(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> list[dict]:
    """The favorited images (joined), for the owner's proofing view."""
    rows = conn.execute(
        "SELECT i.* FROM image_favorites f "
        "JOIN images i ON i.id = f.image_id AND i.tenant_id = f.tenant_id "
        "AND i.gallery_id = f.gallery_id "
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


def comments_by_image(
    conn: sqlite3.Connection,
    gallery_id: int,
    *,
    tenant_id: str | None = None,
) -> dict[int, list[dict]]:
    """Map image_id → its comments (oldest first), for the client gallery view."""
    sql = "SELECT * FROM image_comments WHERE gallery_id = ?"
    params: list = [gallery_id]
    if tenant_id is not None:
        sql += " AND tenant_id = ?"
        params.append(tenant_id)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        out.setdefault(r["image_id"], []).append(dict(r))
    return out


def comments_for_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> list[dict]:
    """All comments (newest first, with the frame filename), for the owner view."""
    rows = conn.execute(
        "SELECT c.*, i.filename, i.position, i.hidden FROM image_comments c "
        "JOIN images i ON i.id = c.image_id AND i.tenant_id = c.tenant_id "
        "AND i.gallery_id = c.gallery_id "
        "WHERE c.gallery_id = ? AND c.tenant_id = ? ORDER BY c.id DESC",
        (gallery_id, tenant_id),
    ).fetchall()
    return [dict(r) for r in rows]


def selection_packet(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict | None:
    """Album/print handoff packet built from the client's favorites and notes.

    This is intentionally a read model over the proofing tables so the studio gets
    a richer handoff without another status table to maintain.
    """
    gallery = conn.execute(
        "SELECT id, title, client_name, selections_submitted_at "
        "FROM galleries WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    if not gallery:
        return None

    favorites = list_favorites(conn, tenant_id, gallery_id)
    comments = comments_for_gallery(conn, tenant_id, gallery_id)
    comments = sorted(comments, key=lambda c: (c.get("position") or 0, c["id"]))

    notes_by_image: dict[int, list[dict]] = defaultdict(list)
    for comment in comments:
        notes_by_image[comment["image_id"]].append(comment)

    favorite_items = [
        {
            "id": fav["id"],
            "filename": fav["filename"],
            "position": fav["position"],
            "hidden": bool(fav["hidden"]),
            "comment_count": len(notes_by_image.get(fav["id"], [])),
            "notes": notes_by_image.get(fav["id"], []),
        }
        for fav in favorites
    ]
    favorite_ids = {fav["id"] for fav in favorites}
    unselected_comments = [
        comment for comment in comments if comment["image_id"] not in favorite_ids
    ]
    submitted_at = gallery["selections_submitted_at"]
    if submitted_at:
        status = "submitted"
        status_label = "Selections submitted"
        next_action = "Build the album or print offer from this packet."
    elif favorites or comments:
        status = "in_progress"
        status_label = "Selection in progress"
        next_action = "Wait for the client to submit, or use the live packet now."
    else:
        status = "empty"
        status_label = "No selections yet"
        next_action = "Send the gallery link and ask the client to heart their favorites."

    return {
        "gallery_id": gallery_id,
        "gallery_title": gallery["title"],
        "client_name": gallery["client_name"],
        "submitted_at": submitted_at,
        "status": status,
        "status_label": status_label,
        "next_action": next_action,
        "favorite_count": len(favorite_items),
        "comment_count": len(comments),
        "commented_frame_count": len({comment["image_id"] for comment in comments}),
        "favorites": favorite_items,
        "comments": comments,
        "unselected_comments": unselected_comments,
    }


def selection_packet_text(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> str:
    """Plain-text proofing handoff for Lightroom/editor/album-design workflows."""
    packet = selection_packet(conn, tenant_id, gallery_id)
    if not packet:
        return ""

    lines = [
        f"Selection packet: {packet['gallery_title']}",
        f"Client: {packet['client_name'] or 'Not set'}",
        f"Status: {packet['status_label']}",
        f"Favorites: {packet['favorite_count']}",
        f"Notes: {packet['comment_count']} across {packet['commented_frame_count']} frame(s)",
    ]
    if packet["submitted_at"]:
        lines.append(f"Submitted: {packet['submitted_at']}")

    lines.extend(["", "Favorites"])
    if packet["favorites"]:
        for item in packet["favorites"]:
            hidden = " (hidden)" if item["hidden"] else ""
            notes = f" - {item['comment_count']} note(s)" if item["comment_count"] else ""
            lines.append(f"- {item['filename']}{hidden}{notes}")
    else:
        lines.append("- None yet")

    lines.extend(["", "Notes"])
    if packet["comments"]:
        for comment in packet["comments"]:
            who = comment["author_name"] or "Client"
            hidden = " (hidden)" if comment.get("hidden") else ""
            lines.append(f"- {comment['filename']}{hidden} - {who}: {comment['body']}")
    else:
        lines.append("- None yet")

    return "\n".join(lines) + "\n"

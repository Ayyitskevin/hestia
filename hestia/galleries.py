"""Native multi-tenant gallery + image hosting — the re-platformed Mise core.

In the single-studio suite, galleries live in Mise (single-tenant, integer ids,
shared local disk). For a multi-tenant SaaS that does not work, so Hestia hosts
galleries itself: tenant-scoped rows here, blobs in :mod:`hestia.storage`. This
is the part of Hestia that is genuinely a new product rather than orchestration.
"""

from __future__ import annotations

import re
import sqlite3
from typing import BinaryIO

from .automations import emit_event
from .storage import Storage, image_key

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Content types we'll serve INLINE. Upload content_type is client-controlled, so a
# malicious "image" declared text/html would otherwise render as a page on our own
# origin (stored XSS). Anything not on this allowlist is served as an opaque
# download instead of being interpreted by the browser.
# Note: SVG is deliberately excluded — it can carry inline <script>, so we never
# render it on our origin (it falls through to an octet-stream download).
_INLINE_IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/webp", "image/gif", "image/avif",
    "image/heic", "image/heif", "image/tiff", "image/bmp",
})


def safe_inline_type(content_type: str | None) -> str:
    """The media type to serve INLINE for a stored image. Real raster types pass
    through; anything else (notably text/html) becomes octet-stream so the browser
    downloads it rather than executing it on our origin."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct if ct in _INLINE_IMAGE_TYPES else "application/octet-stream"


def _slugify(value: str) -> str:
    return _SLUG_RE.sub("-", (value or "").strip().lower()).strip("-") or "gallery"


# ── Galleries ───────────────────────────────────────────────────────────────


def create_gallery(
    conn: sqlite3.Connection,
    *,
    tenant_id: str,
    title: str,
    client_name: str = "",
    pin: str | None = None,
) -> dict:
    base = _slugify(title)
    slug = base
    n = 2
    while conn.execute(
        "SELECT 1 FROM galleries WHERE tenant_id = ? AND slug = ?", (tenant_id, slug)
    ).fetchone():
        slug = f"{base}-{n}"
        n += 1
    cur = conn.execute(
        "INSERT INTO galleries (tenant_id, slug, title, client_name, pin) VALUES (?, ?, ?, ?, ?)",
        (tenant_id, slug, title, client_name, pin or None),
    )
    return get_gallery(conn, tenant_id, cur.lastrowid)


def get_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM galleries WHERE id = ? AND tenant_id = ?", (gallery_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def get_gallery_by_slug(conn: sqlite3.Connection, tenant_id: str, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM galleries WHERE tenant_id = ? AND slug = ?", (tenant_id, slug)
    ).fetchone()
    return dict(row) if row else None


def record_gallery_view(conn: sqlite3.Connection, gallery_id: int) -> None:
    """Count a client opening the gallery (delivery or proofing page) + stamp last seen.
    By gallery id: the public routes have already resolved the gallery via its token/PIN."""
    conn.execute("UPDATE galleries SET view_count = view_count + 1, "
                 "last_viewed_at = datetime('now') WHERE id = ?", (gallery_id,))


def record_gallery_download(conn: sqlite3.Connection, gallery_id: int) -> None:
    """Count a download action — the whole-set zip, or an individual original."""
    conn.execute("UPDATE galleries SET download_count = download_count + 1 WHERE id = ?",
                 (gallery_id,))


def cover_storage_key(conn: sqlite3.Connection, tenant_id: str, gallery: dict) -> str | None:
    """The storage key to show as a gallery's cover thumbnail: the chosen ``cover_image_id``
    when it's still a visible frame, else the first visible frame, else None (empty gallery)."""
    cid = gallery.get("cover_image_id")
    if cid:
        row = conn.execute(
            "SELECT storage_key FROM images "
            "WHERE id = ? AND gallery_id = ? AND tenant_id = ? AND hidden = 0",
            (cid, gallery["id"], tenant_id),
        ).fetchone()
        if row:
            return row["storage_key"]
    row = conn.execute(
        "SELECT storage_key FROM images WHERE gallery_id = ? AND tenant_id = ? AND hidden = 0 "
        "ORDER BY position, id LIMIT 1",
        (gallery["id"], tenant_id),
    ).fetchone()
    return row["storage_key"] if row else None


def list_galleries(conn: sqlite3.Connection, tenant_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM galleries WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
    ).fetchall()
    from .albums import album_status_for_gallery  # lazy: albums imports galleries

    out = []
    for r in rows:
        g = dict(r)
        g["image_count"] = image_count(conn, g["id"])
        g["cover_key"] = cover_storage_key(conn, tenant_id, g)
        g["album_status"] = album_status_for_gallery(conn, tenant_id, g["id"])
        out.append(g)
    return out


def publish_gallery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> None:
    conn.execute(
        "UPDATE galleries SET status = 'published', published_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    )
    g = conn.execute(
        "SELECT title, project_id FROM galleries WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    if g:
        emit_event(conn, tenant_id=tenant_id, event="gallery.published",
                   context={"project_id": g["project_id"], "title": g["title"]})


def submit_selections(conn: sqlite3.Connection, *, tenant_id: str, gallery_id: int) -> bool:
    """Client finalizes their proofing picks — a one-way "I'm done, these are my
    favorites" signal that closes the gallery → album/offer handoff.

    Claim-before-act: the guarded UPDATE only matches a not-yet-submitted gallery
    (``selections_submitted_at IS NULL``), so just the FIRST submit wins (rowcount
    == 1). A double-submit or a re-opened link is a no-op that returns False and
    never re-stamps or re-notifies. On the winning submit it emits
    ``gallery.selections_submitted`` so the owner's automations can fire."""
    cur = conn.execute(
        "UPDATE galleries SET selections_submitted_at = datetime('now') "
        "WHERE id = ? AND tenant_id = ? AND selections_submitted_at IS NULL",
        (gallery_id, tenant_id),
    )
    if cur.rowcount != 1:
        return False
    g = conn.execute(
        "SELECT title, project_id FROM galleries WHERE id = ? AND tenant_id = ?",
        (gallery_id, tenant_id),
    ).fetchone()
    favs = conn.execute(
        "SELECT COUNT(*) AS n FROM image_favorites WHERE gallery_id = ?", (gallery_id,)
    ).fetchone()
    if g:
        emit_event(conn, tenant_id=tenant_id, event="gallery.selections_submitted",
                   context={"project_id": g["project_id"], "title": g["title"],
                            "favorite_count": favs["n"] if favs else 0})
    return True


# ── Images ──────────────────────────────────────────────────────────────────


def _dimensions(data: bytes) -> tuple[int | None, int | None]:
    """Best-effort image dimensions (Pillow if available, else unknown)."""
    try:
        import io

        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(data)) as im:
            return im.width, im.height
    except Exception:
        return None, None


def add_image(
    conn: sqlite3.Connection,
    storage: Storage,
    *,
    tenant_id: str,
    gallery_id: int,
    filename: str,
    fileobj: BinaryIO,
    content_type: str = "application/octet-stream",
) -> dict:
    data = fileobj.read()
    width, height = _dimensions(data)
    position = image_count(conn, gallery_id)
    ext = filename.rsplit(".", 1)[-1] if "." in filename else "bin"
    # Insert first to get the id, then compute key and persist the blob.
    cur = conn.execute(
        """
        INSERT INTO images (gallery_id, tenant_id, filename, storage_key, content_type,
                            width, height, bytes, position)
        VALUES (?, ?, ?, '', ?, ?, ?, ?, ?)
        """,
        (gallery_id, tenant_id, filename, content_type, width, height, len(data), position),
    )
    image_id = cur.lastrowid
    key = image_key(tenant_id, gallery_id, image_id, ext)
    import io

    storage.put(key, io.BytesIO(data), content_type)
    conn.execute("UPDATE images SET storage_key = ? WHERE id = ?", (key, image_id))
    # First image becomes the cover.
    conn.execute(
        "UPDATE galleries SET cover_image_id = COALESCE(cover_image_id, ?) WHERE id = ?",
        (image_id, gallery_id),
    )
    row = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
    return dict(row)


def list_images(conn: sqlite3.Connection, gallery_id: int, *,
                include_hidden: bool = True) -> list[dict]:
    """Images in a gallery. ``include_hidden`` defaults True (the owner sees every frame,
    culled ones marked); the client gallery and delivery pass False to drop culled frames."""
    sql = "SELECT * FROM images WHERE gallery_id = ?"
    if not include_hidden:
        sql += " AND hidden = 0"
    sql += " ORDER BY position, id"
    return [dict(r) for r in conn.execute(sql, (gallery_id,)).fetchall()]


def image_count(conn: sqlite3.Connection, gallery_id: int, *, include_hidden: bool = True) -> int:
    sql = "SELECT COUNT(*) AS n FROM images WHERE gallery_id = ?"
    if not include_hidden:
        sql += " AND hidden = 0"
    row = conn.execute(sql, (gallery_id,)).fetchone()
    return row["n"] if row else 0


def set_image_hidden(conn: sqlite3.Connection, tenant_id: str, image_id: int, hidden: bool) -> None:
    """Hide (cull) or restore a single image — tenant-scoped, reversible."""
    conn.execute(
        "UPDATE images SET hidden = ? WHERE id = ? AND tenant_id = ?",
        (1 if hidden else 0, image_id, tenant_id),
    )


def set_cover_image(conn: sqlite3.Connection, tenant_id: str, gallery_id: int, image_id: int) -> bool:
    """Set a gallery's cover to one of its own visible frames. Returns False if the image
    isn't part of this gallery/tenant or is hidden (a culled frame shouldn't be the cover).
    Tenant-scoped."""
    row = conn.execute(
        "SELECT 1 FROM images WHERE id = ? AND gallery_id = ? AND tenant_id = ? AND hidden = 0",
        (image_id, gallery_id, tenant_id),
    ).fetchone()
    if not row:
        return False
    conn.execute(
        "UPDATE galleries SET cover_image_id = ? WHERE id = ? AND tenant_id = ?",
        (image_id, gallery_id, tenant_id),
    )
    return True


def apply_cull(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> int:
    """Hide every frame the vision pass currently flags (near-duplicates + likely blinks).
    Returns how many were newly hidden. Reversible per-image; re-running is harmless."""
    from .vision import cull_summary
    cull = cull_summary(conn, tenant_id, gallery_id)
    ids = sorted(set(cull.get("duplicate_ids") or set()) | set(cull.get("blink_ids") or set()))
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE images SET hidden = 1 WHERE tenant_id = ? AND gallery_id = ? "
        f"AND hidden = 0 AND id IN ({placeholders})",
        (tenant_id, gallery_id, *ids),
    )
    return cur.rowcount


def apply_quality_cull(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> int:
    """Hide every still-visible frame the vision pass flags as a likely technical reject
    (soft / under- or over-exposed, from the exposure & sharpness sub-scores). Returns how
    many were newly hidden. Reversible per-image; re-running is harmless."""
    from .vision import flagged_image_ids
    ids = sorted(flagged_image_ids(conn, tenant_id, gallery_id))
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE images SET hidden = 1 WHERE tenant_id = ? AND gallery_id = ? "
        f"AND hidden = 0 AND id IN ({placeholders})",
        (tenant_id, gallery_id, *ids),
    )
    return cur.rowcount


def get_image(conn: sqlite3.Connection, tenant_id: str, image_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM images WHERE id = ? AND tenant_id = ?", (image_id, tenant_id)
    ).fetchone()
    return dict(row) if row else None


def image_manifest(conn: sqlite3.Connection, storage: Storage, gallery_id: int, *, limit: int | None = None) -> list[dict]:
    """Image descriptors the engines consume: id, filename, storage key, URL."""
    images = list_images(conn, gallery_id)
    if limit:
        images = images[:limit]
    return [
        {
            "id": img["id"],
            "filename": img["filename"],
            "storage_key": img["storage_key"],
            "url": storage.public_path(img["storage_key"]),
            "content_type": img["content_type"],
        }
        for img in images
    ]

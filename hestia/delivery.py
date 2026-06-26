"""Digital delivery — hand the client their finished high-res gallery.

One unguessable link per gallery (the same token model as offers and portals): the
owner enables delivery, shares the link, and the client downloads the originals —
each file individually or the whole set as a single zip. No client login, no new
password surface. The link is opt-in (nullable token) and rotatable; regenerating
mints a fresh one and instantly revokes the old link.
"""

from __future__ import annotations

import io
import sqlite3
import zipfile

from .config import Settings
from .crypto import new_session_token
from .galleries import get_gallery
from .storage import Storage


def enable_delivery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> str | None:
    """Ensure the gallery has a delivery token, minting one if absent. Idempotent —
    an existing token is preserved so the link the client already has keeps working."""
    gallery = get_gallery(conn, tenant_id, gallery_id)
    if not gallery:
        return None
    if gallery.get("delivery_token"):
        return gallery["delivery_token"]
    token = new_session_token()
    conn.execute(
        "UPDATE galleries SET delivery_token = ? WHERE id = ? AND tenant_id = ?",
        (token, gallery_id, tenant_id),
    )
    return token


def regenerate_delivery_token(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> str | None:
    """Rotate the delivery token, revoking the previous link."""
    if not get_gallery(conn, tenant_id, gallery_id):
        return None
    token = new_session_token()
    conn.execute(
        "UPDATE galleries SET delivery_token = ? WHERE id = ? AND tenant_id = ?",
        (token, gallery_id, tenant_id),
    )
    return token


def get_gallery_by_delivery_token(conn: sqlite3.Connection, token: str) -> dict | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM galleries WHERE delivery_token = ?", (token,)
    ).fetchone()
    return dict(row) if row else None


def delivery_url(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}/d/{token}"


def zip_gallery(storage: Storage, images: list[dict]) -> bytes:
    """Bundle the gallery's originals into one in-memory zip, keyed by filename.

    Photos are already compressed, so we store (no deflate) — faster and no size win
    from re-compressing. Duplicate filenames are disambiguated so nothing clobbers."""
    buf = io.BytesIO()
    seen: dict[str, int] = {}
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for img in images:
            name = img.get("filename") or f"image-{img['id']}"
            if name in seen:
                seen[name] += 1
                stem, dot, ext = name.rpartition(".")
                name = f"{stem}-{seen[name]}{dot}{ext}" if dot else f"{name}-{seen[name]}"
            else:
                seen[name] = 0
            try:
                zf.writestr(name, storage.open(img["storage_key"]))
            except FileNotFoundError:
                continue
    return buf.getvalue()

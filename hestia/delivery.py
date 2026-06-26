"""Digital delivery — hand the client their finished high-res gallery.

One unguessable link per gallery (the same token model as offers and portals): the
owner enables delivery, shares the link, and the client downloads the originals —
each file individually or the whole set as a single zip. No client login, no new
password surface. The link is opt-in (nullable token) and rotatable; regenerating
mints a fresh one and instantly revokes the old link.
"""

from __future__ import annotations

import sqlite3
import zipfile
from collections.abc import Iterator

from .config import Settings
from .crypto import new_session_token
from .galleries import get_gallery
from .storage import Storage


def enable_delivery(conn: sqlite3.Connection, tenant_id: str, gallery_id: int) -> str | None:
    """Ensure the gallery has a delivery token, minting one if absent. Idempotent and
    race-safe: the mint only writes when the token is still empty, so two concurrent
    'enable' requests can't overwrite each other and strand the first shared link —
    the loser reads back and returns the winner's token."""
    gallery = get_gallery(conn, tenant_id, gallery_id)
    if not gallery:
        return None
    if gallery.get("delivery_token"):
        return gallery["delivery_token"]
    token = new_session_token()
    cur = conn.execute(
        "UPDATE galleries SET delivery_token = ? WHERE id = ? AND tenant_id = ? "
        "AND (delivery_token IS NULL OR delivery_token = '')",
        (token, gallery_id, tenant_id),
    )
    if cur.rowcount:
        return token
    fresh = get_gallery(conn, tenant_id, gallery_id)  # lost a concurrent mint
    return fresh["delivery_token"] if fresh else None


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


def _dedup_name(img: dict, seen: dict[str, int]) -> str:
    """A unique archive name per file, so duplicate filenames don't clobber."""
    name = img.get("filename") or f"image-{img['id']}"
    if name in seen:
        seen[name] += 1
        stem, dot, ext = name.rpartition(".")
        return f"{stem}-{seen[name]}{dot}{ext}" if dot else f"{name}-{seen[name]}"
    seen[name] = 0
    return name


class _ZipSink:
    """Write-only, non-seekable buffer for streaming a zip out chunk by chunk.

    No ``seek`` method, so :class:`zipfile.ZipFile` falls back to stream mode (data
    descriptors) instead of seeking back to patch headers — which lets us flush each
    entry to the client as it's written rather than holding the whole archive."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._chunks.append(bytes(data))
        self._pos += len(data)
        return len(data)

    def flush(self) -> None:
        pass

    def tell(self) -> int:
        return self._pos

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def iter_zip(storage: Storage, images: list[dict]) -> Iterator[bytes]:
    """Stream a ZIP_STORED archive of the originals, one file in memory at a time
    (bounded by the largest single image) so a multi-GB gallery never balloons RAM.
    Photos are already compressed, so storing (no deflate) is faster with no size win."""
    sink = _ZipSink()
    seen: dict[str, int] = {}
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
        for img in images:
            name = _dedup_name(img, seen)
            try:
                zf.writestr(name, storage.open(img["storage_key"]))
            except FileNotFoundError:
                continue
            chunk = sink.drain()
            if chunk:
                yield chunk
    tail = sink.drain()  # central directory, written on close
    if tail:
        yield tail


def zip_gallery(storage: Storage, images: list[dict]) -> bytes:
    """Materialize the streamed zip as a single blob (kept for callers/tests that
    want the whole archive at once)."""
    return b"".join(iter_zip(storage, images))

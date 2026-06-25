"""Object-storage abstraction for native gallery hosting.

The research showed the suite couples on a *shared local disk* keyed by Mise's
gallery id — a homelab assumption that breaks across cloud hosts. A multi-tenant
SaaS needs real object storage. This module is that seam: a tiny interface with a
local-filesystem backend today and an S3/R2 backend in Phase 1. Keys are always
tenant-scoped (``<tenant_id>/<gallery_id>/<image_id>.<ext>``) so nothing leaks
across studios.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO, Protocol


class Storage(Protocol):
    """Minimal blob store. Backends: local now, S3/R2 in Phase 1."""

    def put(self, key: str, data: BinaryIO, content_type: str = "application/octet-stream") -> str:
        ...

    def open(self, key: str) -> bytes:
        ...

    def exists(self, key: str) -> bool:
        ...

    def delete(self, key: str) -> None:
        ...

    def public_path(self, key: str) -> str:
        """A path the app can serve/sign. Local → /media/<key>; S3 → signed URL."""
        ...


class LocalStorage:
    """Filesystem backend rooted at ``media_dir``. Serves via the /media route."""

    backend = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _full(self, key: str) -> Path:
        # Defend against traversal: keys are app-generated, but be safe anyway.
        safe = Path(key.lstrip("/"))
        if ".." in safe.parts:
            raise ValueError(f"unsafe storage key: {key!r}")
        return self.root / safe

    def put(self, key: str, data: BinaryIO, content_type: str = "application/octet-stream") -> str:
        dest = self._full(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            shutil.copyfileobj(data, fh)
        return key

    def open(self, key: str) -> bytes:
        return self._full(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._full(key).is_file()

    def delete(self, key: str) -> None:
        path = self._full(key)
        if path.is_file():
            path.unlink()

    def public_path(self, key: str) -> str:
        return f"/media/{key}"


def build_storage(settings) -> Storage:
    """Construct the configured storage backend."""
    if settings.storage_backend == "s3":  # pragma: no cover - Phase 1
        raise NotImplementedError(
            "S3/R2 storage backend is a Phase 1 deliverable; set HESTIA_STORAGE_BACKEND=local"
        )
    return LocalStorage(settings.media_dir)


def image_key(tenant_id: str, gallery_id: int, image_id: int, ext: str) -> str:
    ext = (ext or "bin").lstrip(".").lower()
    return f"{tenant_id}/{gallery_id}/{image_id}.{ext}"

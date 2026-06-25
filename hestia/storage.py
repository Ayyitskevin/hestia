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


class S3Storage:
    """S3-compatible backend (AWS S3, Cloudflare R2, MinIO).

    Credentials come from the standard AWS chain (``AWS_ACCESS_KEY_ID`` /
    ``AWS_SECRET_ACCESS_KEY`` env or instance role). Set ``endpoint_url`` for R2 /
    MinIO. ``public_base_url`` (a CDN or public bucket) yields direct URLs;
    otherwise ``public_path`` returns a short-lived presigned GET URL.
    """

    backend = "s3"

    def __init__(self, bucket: str, *, region: str = "us-east-1", endpoint_url: str = "",
                 public_base_url: str = "", client=None):
        if not bucket:
            raise ValueError("S3 storage requires HESTIA_S3_BUCKET")
        self.bucket = bucket
        self.public_base_url = public_base_url.rstrip("/")
        if client is not None:
            self._client = client
        else:  # pragma: no cover - exercised via injected client in tests
            import boto3

            self._client = boto3.client("s3", region_name=region,
                                        endpoint_url=endpoint_url or None)

    def put(self, key: str, data: BinaryIO, content_type: str = "application/octet-stream") -> str:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data.read(),
                                ContentType=content_type)
        return key

    def open(self, key: str) -> bytes:
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return resp["Body"].read()

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def public_path(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
        )


def build_storage(settings) -> Storage:
    """Construct the configured storage backend."""
    if settings.storage_backend == "s3":
        return S3Storage(
            settings.s3_bucket, region=settings.s3_region,
            endpoint_url=settings.s3_endpoint_url,
            public_base_url=settings.s3_public_base_url,
        )
    return LocalStorage(settings.media_dir)


def image_key(tenant_id: str, gallery_id: int, image_id: int, ext: str) -> str:
    ext = (ext or "bin").lstrip(".").lower()
    return f"{tenant_id}/{gallery_id}/{image_id}.{ext}"

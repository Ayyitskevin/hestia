"""Object-storage abstraction for native gallery hosting.

The research showed the suite couples on a *shared local disk* keyed by Mise's
gallery id — a homelab assumption that breaks across cloud hosts. A multi-tenant
SaaS needs real object storage. This module is that seam: a tiny interface with a
local-filesystem backend today and an S3/R2 backend behind env. Keys are always
tenant-scoped (``<tenant_id>/<gallery_id>/<image_id>.<ext>``) so nothing leaks
across studios.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import BinaryIO, Protocol


class Storage(Protocol):
    """Minimal blob store. Backends: local filesystem, S3/R2 behind env."""

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

    def image_url(self, image: dict) -> str:
        """A client-facing URL for an image *row*. Local serves through the app's
        /media route keyed by the row's unguessable ``access_token`` (never the
        enumerable storage key); S3 returns a short-lived presigned URL. Use this for any
        image shown to a client — the raw ``public_path(storage_key)`` is owner-only."""
        ...

    def thumb_url(self, image: dict) -> str:
        """A client-facing URL for the image's downscaled *browse thumbnail*, or the
        full-image URL when the row has no thumbnail (pre-migration uploads, or a frame
        whose thumbnailing failed). Use this for grids and proofing — where a client
        loads many frames at once — and reserve :meth:`image_url` / full downloads for
        the single large view. Same access control as the full image."""
        ...

    def file_path(self, key: str) -> str | None:
        """Local filesystem path for a key, so the app can stream it from disk with a
        ``FileResponse`` instead of reading the whole blob into memory. Returns ``None``
        for remote backends (S3), which must be proxied or redirected instead."""
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

    def image_url(self, image: dict) -> str:
        # /media/<token>: no slashes, so serve_media routes it to the token lookup
        # (which enforces published/not-hidden), not the owner-only storage-key path.
        token = image["access_token"] if "access_token" in image.keys() else ""
        return f"/media/{token}" if token else f"/media/{image['storage_key']}"

    def thumb_url(self, image: dict) -> str:
        # The thumbnail is served through the same token route (same access control),
        # tagged ?s=t so serve_media returns the small JPEG. No thumbnail → full image.
        keys = image.keys()
        token = image["access_token"] if "access_token" in keys else ""
        has_thumb = "thumb_key" in keys and image["thumb_key"]
        return f"/media/{token}?s=t" if (token and has_thumb) else self.image_url(image)

    def file_path(self, key: str) -> str | None:
        return str(self._full(key))


class S3Storage:
    """S3-compatible backend (AWS S3, Cloudflare R2, MinIO).

    Credentials come from the standard AWS chain (``AWS_ACCESS_KEY_ID`` /
    ``AWS_SECRET_ACCESS_KEY`` env or instance role). Set ``endpoint_url`` for R2 /
    MinIO. Media buckets must remain private; ``public_path`` returns a
    short-lived presigned GET URL.
    """

    backend = "s3"

    def __init__(self, bucket: str, *, region: str = "us-east-1", endpoint_url: str = "",
                 public_base_url: str = "", client=None):
        if not bucket:
            raise ValueError("S3 storage requires HESTIA_S3_BUCKET")
        if public_base_url:
            raise ValueError(
                "HESTIA_S3_PUBLIC_BASE_URL is unsafe: public object URLs bypass "
                "gallery visibility and per-image capability checks"
            )
        self.bucket = bucket
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
        return self._client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=3600
        )

    def image_url(self, image: dict) -> str:
        # Presigned URLs are short-lived and the bucket is private, so knowing an
        # enumerable storage key never grants permanent public access.
        return self.public_path(image["storage_key"])

    def thumb_url(self, image: dict) -> str:
        # Offload thumbnail delivery to S3 too (presigned), falling back to the full
        # image when there's no thumbnail. Client browsers hit S3 directly, not the app.
        thumb = image["thumb_key"] if "thumb_key" in image.keys() else None
        return self.public_path(thumb) if thumb else self.image_url(image)

    def file_path(self, key: str) -> str | None:
        return None  # remote object store — no local file to stream


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

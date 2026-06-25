"""``/healthz`` — liveness + self checks (one app, no fleet to aggregate)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import __version__
from ..db import connect
from .deps import settings_of

router = APIRouter()


@router.get("/healthz")
def healthz(request: Request) -> dict:
    settings = settings_of(request)

    db_ok = True
    try:
        conn = connect(settings.db_path)
        conn.execute("SELECT 1")
        conn.close()
    except Exception:
        db_ok = False

    storage_ok = settings.media_dir.exists() or settings.storage_backend != "local"

    return {
        "service": "hestia",
        "version": __version__,
        "status": "ok" if db_ok and storage_ok else "degraded",
        "db": "ok" if db_ok else "error",
        "storage": settings.storage_backend if storage_ok else "error",
        "vision_backend": settings.vision_backend,
    }

"""``/healthz`` — liveness + self checks (one app, no fleet to aggregate)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import __version__
from ..db import connect
from ..domains import get_tenant_by_custom_domain
from ..hosted import RESERVED_SUBDOMAINS
from ..tenants import get_tenant_by_slug
from .deps import settings_of

router = APIRouter()


@router.get("/internal/tls-check")
def tls_check(request: Request, domain: str = "") -> Response:
    """Caddy on-demand TLS gate. Caddy queries this before issuing a certificate for
    a hostname; we approve (200) only the apex, a ``{slug}.{HESTIA_DOMAIN}`` whose
    slug is a real tenant, or a verified custom domain — never an arbitrary host,
    which would let anyone make us mint certs against ACME rate limits. Anything
    else is 404 so Caddy refuses issuance."""
    settings = settings_of(request)
    host = (domain or "").strip().lower().rstrip(".")
    base = (getattr(settings, "hosted_domain", "") or "").strip().lower()
    if not host:
        return Response(status_code=404)
    if base and host == base:
        return Response(status_code=200)                       # apex / marketing site
    conn = connect(settings.db_path)
    try:
        if base and host.endswith(f".{base}"):
            slug = host[: -(len(base) + 1)].strip(".")
            if (slug and "." not in slug and slug not in RESERVED_SUBDOMAINS
                    and get_tenant_by_slug(conn, slug)):
                return Response(status_code=200)               # real tenant subdomain
            return Response(status_code=404)
        if get_tenant_by_custom_domain(conn, host):
            return Response(status_code=200)                   # verified custom domain
    finally:
        conn.close()
    return Response(status_code=404)


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


@router.get("/readyz")
def readyz(request: Request):
    """Readiness: can we actually serve? DB queryable, schema migrated, storage present."""
    settings = settings_of(request)
    checks = {"db": False, "migrations": False, "storage": False}
    try:
        conn = connect(settings.db_path)
        conn.execute("SELECT 1")
        checks["migrations"] = conn.execute(
            "SELECT COUNT(*) AS n FROM schema_migrations"
        ).fetchone()["n"] > 0
        conn.close()
        checks["db"] = True
    except Exception:
        pass
    checks["storage"] = settings.media_dir.exists() or settings.storage_backend != "local"
    ready = all(checks.values())
    return JSONResponse({"ready": ready, "checks": checks},
                        status_code=200 if ready else 503)

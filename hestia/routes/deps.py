"""Shared helpers for route modules: config, DB, templates, auth, storage."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth import OWNER, AuthContext, context_from_session, resolve_context
from ..config import Settings
from ..csrf import csrf_token_for
from ..db import get_db
from ..storage import Storage

# Media is revocable client content: a client-token or delivery link can be rotated
# or expire, so we must NOT let a browser display a stale copy afterwards. `no-cache`
# (store, but revalidate before every use) gets us cheap 304 revalidation while keeping
# revocation instant — the caller re-runs access control on each request, so a revoked
# link returns 403 on revalidation, never a 304. `private` keeps shared proxies out.
_MEDIA_CACHE = "private, no-cache"


def image_response(request: Request, storage: Storage, key: str, *, media_type: str):
    """Serve a stored image key with the right performance + caching for the backend.

    Local storage streams straight from disk via ``FileResponse`` (no full-file read
    into the app's memory — the OOM/bandwidth cliff for big galleries — with HTTP Range
    handled for free). Remote backends proxy the bytes. The ETag is the content-addressed
    key (a key's bytes never change), so a matching ``If-None-Match`` short-circuits to a
    tiny 304 instead of re-sending the frame. Callers resolve access control BEFORE
    calling this, so revalidation re-checks authorization and revocation stays instant."""
    etag = f'"{key}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304,
                        headers={"ETag": etag, "Cache-Control": _MEDIA_CACHE})
    headers = {"Cache-Control": _MEDIA_CACHE, "ETag": etag}
    path = storage.file_path(key)
    if path is not None:
        if not os.path.exists(path):
            return Response(status_code=404)
        # FileResponse setdefault()s its stat-based etag, so our key-based one wins.
        return FileResponse(path, media_type=media_type, headers=headers)
    try:
        data = storage.open(key)
    except FileNotFoundError:
        return Response(status_code=404)
    return Response(content=data, media_type=media_type, headers=headers)


def settings_of(request: Request) -> Settings:
    return request.app.state.settings


def templates_of(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def storage_of(request: Request) -> Storage:
    return request.app.state.storage


@contextmanager
def db_conn(request: Request) -> Iterator:
    with get_db(settings_of(request).db_path) as conn:
        yield conn


def render(request: Request, template: str, *, status_code: int = 200, **context):
    base = {
        "settings": settings_of(request),
        "auth": context.pop("auth", None),
        "csrf_token": csrf_token_for(request),
        "csp_nonce": getattr(request.state, "csp_nonce", ""),
    }
    base.update(context)
    return templates_of(request).TemplateResponse(
        request=request, name=template, context=base, status_code=status_code
    )


def auth_context(request: Request) -> AuthContext | None:
    settings = settings_of(request)
    with get_db(settings.db_path) as conn:
        return resolve_context(conn, settings, request)


def tenant_user(request: Request, conn) -> AuthContext | None:
    """Resolve the authenticated studio user for a request, or None.

    Returns None when there is no session or the session is not bound to a
    tenant (e.g. an admin-only session). Owner/admin routes use this to guard
    the request and read ``auth.tenant``; role gating is :func:`owner_only`.
    """
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth


def owner_only(auth: AuthContext | None):
    """Return a RedirectResponse when ``auth`` is not the account owner (a
    secondary admin reaching an owner-only route is bounced to Site settings
    with a forbidden banner), or None to continue. Call after ``tenant_user``."""
    if not auth:
        return RedirectResponse("/login", status_code=303)
    if auth.role != OWNER:
        return RedirectResponse("/settings/site?forbidden=1", status_code=303)
    return None

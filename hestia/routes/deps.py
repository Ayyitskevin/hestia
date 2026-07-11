"""Shared helpers for route modules: config, DB, templates, auth, storage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..auth import AuthContext, context_from_session, resolve_context
from ..config import Settings
from ..csrf import csrf_token_for
from ..db import get_db
from ..storage import Storage


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
    """Resolve the authenticated studio owner for a request, or None.

    Returns None when there is no session or the session is not bound to a
    tenant (e.g. an admin-only session). Owner routes use this to guard the
    request and read ``auth.tenant``.
    """
    auth = context_from_session(conn, request)
    if not auth or not auth.tenant:
        return None
    return auth

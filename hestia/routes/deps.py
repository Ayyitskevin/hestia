"""Shared helpers for route modules: config, DB, templates, auth, storage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import Request
from fastapi.templating import Jinja2Templates

from ..auth import AuthContext, resolve_context
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
    }
    base.update(context)
    return templates_of(request).TemplateResponse(
        request=request, name=template, context=base, status_code=status_code
    )


def auth_context(request: Request) -> AuthContext | None:
    settings = settings_of(request)
    with get_db(settings.db_path) as conn:
        return resolve_context(conn, settings, request)

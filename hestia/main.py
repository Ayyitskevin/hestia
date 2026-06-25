"""Hestia FastAPI application factory — the AI-native studio, one app.

    uvicorn hestia.main:app --port 8500

Wires config, the SQLite control plane, object storage, Jinja templates, static
assets, and the route modules. The AI engines (vision, sales) are in-process
modules, not services — see :mod:`hestia.vision` and :mod:`hestia.sales`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__
from .config import Settings, get_settings
from .db import init_db
from .features import SHOOT_TYPE_LABELS, SHOOT_TYPES
from .routes import (
    admin,
    albums,
    api,
    client,
    content,
    crm,
    galleries,
    health,
    invoices,
    media,
    pay,
    pipeline_ui,
    products,
    studio,
    web,
    webhooks,
)
from .storage import build_storage

log = logging.getLogger("hestia")

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"


def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals.update(
        app_version=__version__,
        shoot_types=SHOOT_TYPES,
        shoot_type_labels=SHOOT_TYPE_LABELS,
    )
    return templates


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    # Initialize eagerly so the app works whether or not the ASGI lifespan runs
    # (e.g. TestClient without a context manager). init_db is idempotent.
    init_db(settings.db_path)
    settings.media_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.db_path)
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        if settings.insecure_secrets:
            log.warning("Insecure default secrets in use: %s — set real values before deploy.",
                        ", ".join(settings.insecure_secrets))
        if settings.vision_backend == "mock":
            log.info("Vision backend = mock (deterministic; set HESTIA_VISION_BACKEND=xai for live).")
        yield

    app = FastAPI(
        title="Hestia",
        version=__version__,
        summary="The AI-native studio for photographers — gallery to paid, in one app.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.templates = _build_templates()
    app.state.storage = build_storage(settings)

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(health.router)
    app.include_router(web.router)
    app.include_router(admin.router)
    app.include_router(crm.router)
    app.include_router(galleries.router)
    app.include_router(albums.router)
    app.include_router(products.router)
    app.include_router(invoices.router)
    app.include_router(content.router)
    app.include_router(studio.router)
    app.include_router(pipeline_ui.router)
    app.include_router(api.router)
    app.include_router(client.router)
    app.include_router(pay.router)
    app.include_router(webhooks.router)
    app.include_router(media.router)

    return app


app = create_app()

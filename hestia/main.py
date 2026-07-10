"""Hestia FastAPI application factory — the AI-native studio, one app.

    uvicorn hestia.main:app --port 8500

Wires config, the SQLite control plane, object storage, Jinja templates, static
assets, and the route modules. The AI engines (vision, sales) are in-process
modules, not services — see :mod:`hestia.vision` and :mod:`hestia.sales`.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .auth import SESSION_COOKIE
from .config import Settings, get_settings
from .csrf import csrf_protect
from .db import init_db
from .features import LEAD_SOURCES, SHOOT_TYPE_LABELS, SHOOT_TYPES
from .jobs import run_worker
from .marketing import LAUNCH_PROOF_STEPS
from .obs import access_log, configure_logging, new_request_id, redact_path
from .ratelimit import RateLimiter
from .routes import (
    admin,
    album_review,
    albums,
    api,
    automations,
    billing,
    book,
    bookings,
    checklists,
    client,
    content,
    contracts,
    crm,
    delivery,
    discounts,
    finances,
    forms,
    galleries,
    giftcards,
    health,
    invoices,
    library,
    media,
    minisessions,
    onboarding,
    packages,
    pay,
    payment_plans,
    pipeline_ui,
    portal,
    products,
    proposals,
    questionnaires,
    recurring,
    scheduler,
    sign,
    studio,
    testimonials,
    web,
    webhooks,
)
from .storage import build_storage

log = logging.getLogger("hestia")

_HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

_CAPABILITY_PATH_PREFIXES = (
    "/portal/",
    "/d/",
    "/pay/",
    "/a/",
    "/sign/",
    "/g/",
    "/s/",
    "/book/",
    "/q/",
    "/t/",
    "/invite/",
    "/verify/",
    "/reset/",
    "/calendar/",
    "/media/",
    "/proposal/",
)
_AUTH_PATHS = frozenset({"/login", "/signup", "/forgot", "/admin"})


def _sensitive_response(request, response) -> bool:
    path = request.url.path
    return bool(
        request.cookies.get(SESSION_COOKIE)
        or response.headers.get("set-cookie")
        or path in _AUTH_PATHS
        or path.startswith(("/admin/", *_CAPABILITY_PATH_PREFIXES))
    )


def _build_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals.update(
        app_version=__version__,
        shoot_types=SHOOT_TYPES,
        shoot_type_labels=SHOOT_TYPE_LABELS,
        lead_sources=LEAD_SOURCES,
        launch_proof_steps=LAUNCH_PROOF_STEPS,
    )
    return templates


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    # Initialize eagerly so the app works whether or not the ASGI lifespan runs
    # (e.g. TestClient without a context manager). init_db is idempotent.
    init_db(settings.db_path)
    settings.media_dir.mkdir(parents=True, exist_ok=True)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings.db_path)
        settings.media_dir.mkdir(parents=True, exist_ok=True)
        for warning in settings.config_warnings:
            log.warning("config: %s", warning)
        if settings.vision_backend == "mock":
            log.info("Vision backend = mock (deterministic; set HESTIA_VISION_BACKEND=xai for live).")
        # Durable background worker: drains the job queue (retries + crash recovery).
        stop = threading.Event()
        worker = threading.Thread(target=run_worker, args=(settings.db_path, settings, stop),
                                  name="hestia-worker", daemon=True)
        worker.start()
        app.state.worker_stop = stop
        log.info("Background job worker started.")
        try:
            yield
        finally:
            stop.set()
            worker.join(timeout=2)

    app = FastAPI(
        title="Hestia",
        version=__version__,
        summary="The AI-native studio for photographers — gallery to paid, in one app.",
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.templates = _build_templates()
    app.state.storage = build_storage(settings)
    app.state.limiter = RateLimiter()

    @app.middleware("http")
    async def security_headers(request, call_next):
        # Per-request nonce: the two inline scripts (the confirm-on-submit delegator in
        # _confirm.html and the pipeline live-poller) carry it, so script-src can stay
        # strict — no 'unsafe-inline', so an injected inline script won't execute.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        # HSTS: pin clients to HTTPS for a year (browsers only honor it over TLS, so it's
        # inert on the internal http hop). setdefault so an edge proxy can still override.
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        # Powerful features Hestia never uses — deny them so a future injection can't reach
        # the camera/mic/location/Payment-Request APIs (Stripe Checkout is a redirect, not
        # the Payment Request API, so payment=() is safe).
        resp.headers.setdefault("Permissions-Policy",
                                "geolocation=(), microphone=(), camera=(), payment=(), usb=()")
        # Isolate the browsing context: a cross-origin opener can't reference our window.
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # Content-Security-Policy. script-src is nonce-only (strict). style-src keeps
        # 'unsafe-inline' for the many low-risk inline style= attributes (nonces don't
        # apply to inline styles); img-src allows data: for the emoji favicon.
        resp.headers.setdefault("Content-Security-Policy", "; ".join([
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "img-src 'self' data:",
            "style-src 'self' 'unsafe-inline'",
            f"script-src 'self' 'nonce-{nonce}'",
        ]))
        if _sensitive_response(request, resp):
            # Authenticated pages and capability URLs can carry client PII, media,
            # signatures, invoices, or session cookies. Never let a browser or
            # intermediary retain them after access is revoked.
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        return resp

    @app.middleware("http")
    async def request_context(request, call_next):
        rid = request.headers.get("X-Request-ID") or new_request_id()
        request.state.request_id = rid
        start = time.perf_counter()
        response = await call_next(request)
        access_log.info("request", extra={
            "request_id": rid, "method": request.method,
            "path": redact_path(request.url.path),   # never persist client bearer tokens
            "status": response.status_code,
            "duration_ms": round((time.perf_counter() - start) * 1000, 1),
        })
        response.headers["X-Request-ID"] = rid
        return response

    def _wants_json(request) -> bool:
        return request.url.path.startswith(("/api", "/webhooks"))

    def _error_page(request, *, status: int, title: str, message: str):
        return app.state.templates.TemplateResponse(
            request=request, name="error.html", status_code=status,
            context={"title": title, "message": message,
                     "home_url": "/", "home_label": "Take me home"})

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request, exc):
        # A mistyped URL gets a warm branded page instead of raw JSON. API/webhook routes
        # and every other HTTP status keep the plain JSON contract their clients expect.
        if exc.status_code == 404 and not _wants_json(request):
            return _error_page(request, status=404, title="Page not found",
                               message="That page moved, expired, or never existed — "
                                       "let's get you back home.")
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):
        # Never leak a stack trace to a photographer; log it fully for us, show a warm page.
        log.exception("unhandled error at %s", request.url.path)
        if _wants_json(request):
            return JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        return _error_page(request, status=500, title="Something went sideways",
                           message="A gremlin got into the wiring — we've been notified. "
                                   "Give it a moment and try again.")

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # CSRF guards the session-cookie UI (its form POSTs are the exposed surface).
    # Exempt: the bearer-auth JSON API, the public PIN/checkout/inquiry routes,
    # the signature-verified Stripe webhook, and the read-only media/health routes.
    csrf = [Depends(csrf_protect)]

    app.include_router(health.router)
    app.include_router(web.router, dependencies=csrf)
    app.include_router(admin.router, dependencies=csrf)
    app.include_router(crm.router, dependencies=csrf)
    app.include_router(checklists.router, dependencies=csrf)
    app.include_router(galleries.router, dependencies=csrf)
    app.include_router(library.router, dependencies=csrf)
    app.include_router(albums.router, dependencies=csrf)
    app.include_router(products.router, dependencies=csrf)
    app.include_router(invoices.router, dependencies=csrf)
    app.include_router(packages.router, dependencies=csrf)
    app.include_router(proposals.router, dependencies=csrf)
    app.include_router(discounts.router, dependencies=csrf)
    app.include_router(giftcards.router, dependencies=csrf)
    app.include_router(minisessions.router, dependencies=csrf)
    app.include_router(payment_plans.router, dependencies=csrf)
    app.include_router(recurring.router, dependencies=csrf)
    app.include_router(contracts.router, dependencies=csrf)
    app.include_router(questionnaires.router, dependencies=csrf)
    app.include_router(automations.router, dependencies=csrf)
    app.include_router(scheduler.router, dependencies=csrf)
    app.include_router(content.router, dependencies=csrf)
    app.include_router(studio.router, dependencies=csrf)
    app.include_router(bookings.router, dependencies=csrf)
    app.include_router(testimonials.router, dependencies=csrf)
    app.include_router(finances.router, dependencies=csrf)
    app.include_router(billing.router, dependencies=csrf)
    app.include_router(onboarding.router, dependencies=csrf)
    app.include_router(pipeline_ui.router, dependencies=csrf)
    app.include_router(api.router)
    app.include_router(client.router)
    app.include_router(pay.router)
    app.include_router(proposals.public_router)
    app.include_router(sign.router)
    app.include_router(portal.router)
    app.include_router(delivery.router)
    app.include_router(album_review.router)
    app.include_router(forms.router)
    app.include_router(book.router)
    app.include_router(webhooks.router)
    app.include_router(media.router)

    return app


app = create_app()

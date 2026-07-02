"""Structured logging + per-request context (request IDs, access logs).

One JSON line per record on the ``hestia`` logger tree, each request tagged with a
short request id that's also echoed in the ``X-Request-ID`` response header — so a
UI action can be traced to the server-side work (and the audit row) it caused.
Set ``HESTIA_LOG_FORMAT=plain`` for human-readable console logs in dev.
"""

from __future__ import annotations

import json
import logging
import uuid

from .config import Settings

access_log = logging.getLogger("hestia.access")

# Structured fields lifted from a record's ``extra=`` into the JSON line.
_EXTRA_FIELDS = ("request_id", "method", "path", "status", "duration_ms", "tenant_id", "action")

# URL prefixes whose next path segment IS a bearer credential (client-token routes)
# or a tenant identifier (media keys). Logging the raw path would persist a working
# credential in the access log; we keep the route prefix for observability and drop
# everything after it. Mirrors the private list in the /robots.txt route.
_REDACT_PREFIXES = frozenset({
    "portal", "d", "pay", "a", "sign", "g", "s", "book", "q", "t",
    "invite", "verify", "reset", "calendar", "media",
})


def redact_path(path: str) -> str:
    """Strip the credential-bearing tail from a token/media URL path for logging.

    ``/portal/<token>`` → ``/portal/[redacted]``; ``/s/<slug>/<token>`` →
    ``/s/[redacted]``. Non-token paths (``/dashboard``, ``/admin/launch``) are
    returned unchanged so ordinary access logging keeps its detail."""
    segments = path.split("/")           # "/d/tok" → ["", "d", "tok"]
    if len(segments) >= 3 and segments[1] in _REDACT_PREFIXES:
        return f"/{segments[1]}/[redacted]"
    return path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            val = getattr(record, key, None)
            if val is not None:
                data[key] = val
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return json.dumps(data, default=str)


def configure_logging(settings: Settings) -> None:
    """Attach a single JSON (or plain) handler to the ``hestia`` logger. Idempotent."""
    logger = logging.getLogger("hestia")
    if getattr(logger, "_hestia_configured", False):
        return
    handler = logging.StreamHandler()
    if settings.log_format == "plain":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(settings.log_level.upper())
    logger.propagate = False  # own the hestia tree; don't double-log via root
    logger._hestia_configured = True


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]

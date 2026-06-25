"""Structured logging — JSON formatter, request ids, access-log middleware."""

import json
import logging
import sys

from hestia.obs import JsonFormatter, configure_logging, new_request_id


def _record(level=logging.INFO, msg="request", exc=None, **extra):
    rec = logging.LogRecord("hestia.access", level, __file__, 1, msg, None, exc)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_formatter_emits_structured_fields():
    out = JsonFormatter().format(
        _record(request_id="abc", method="GET", path="/x", status=200, duration_ms=1.2))
    data = json.loads(out)  # valid JSON
    assert data["msg"] == "request" and data["level"] == "INFO"
    assert data["logger"] == "hestia.access"
    assert data["request_id"] == "abc" and data["method"] == "GET" and data["status"] == 200


def test_json_formatter_omits_absent_extras():
    data = json.loads(JsonFormatter().format(_record()))
    assert "request_id" not in data and "status" not in data


def test_json_formatter_includes_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        data = json.loads(JsonFormatter().format(
            _record(level=logging.ERROR, msg="oops", exc=sys.exc_info())))
    assert "exc" in data and "boom" in data["exc"]


def test_new_request_id_is_short_and_unique():
    a, b = new_request_id(), new_request_id()
    assert len(a) == 12 and a != b


def test_configure_logging_is_idempotent(settings):
    logger = logging.getLogger("hestia")
    configure_logging(settings)
    before = len(logger.handlers)
    configure_logging(settings)  # repeat calls (every create_app) must not pile up handlers
    assert len(logger.handlers) == before


def test_response_carries_a_request_id(client):
    r = client.get("/healthz")
    assert len(r.headers["X-Request-ID"]) == 12


def test_request_id_is_echoed_when_supplied(client):
    r = client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert r.headers["X-Request-ID"] == "trace-123"

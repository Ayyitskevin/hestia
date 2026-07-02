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


def test_redact_path_strips_token_tails():
    from hestia.obs import redact_path
    # client bearer tokens and media keys → prefix kept, credential dropped
    assert redact_path("/portal/abc123secret") == "/portal/[redacted]"
    assert redact_path("/d/deliverytoken") == "/d/[redacted]"
    assert redact_path("/pay/invtoken") == "/pay/[redacted]"
    assert redact_path("/s/studio-slug/offertoken") == "/s/[redacted]"
    assert redact_path("/g/studio/gallery-slug") == "/g/[redacted]"
    assert redact_path("/calendar/tok.ics") == "/calendar/[redacted]"
    assert redact_path("/media/3f9atenantuuid/12/800.jpg") == "/media/[redacted]"
    assert redact_path("/invite/invtok") == "/invite/[redacted]"
    # ordinary app paths are untouched — access logging keeps its detail
    assert redact_path("/dashboard") == "/dashboard"
    assert redact_path("/admin/launch") == "/admin/launch"
    assert redact_path("/") == "/"
    assert redact_path("/pricing") == "/pricing"


def test_access_log_never_persists_a_client_token(settings, conn, storage, caplog):
    """End-to-end: hitting a real token route must not leave the token in the log."""
    import io

    from starlette.testclient import TestClient

    from hestia.delivery import enable_delivery
    from hestia.galleries import add_image, create_gallery, publish_gallery
    from hestia.main import create_app
    from hestia.tenants import create_tenant

    t = create_tenant(conn, name="Log Studio", shoot_type="wedding")
    g = create_gallery(conn, tenant_id=t["id"], title="Finals")
    add_image(conn, storage, tenant_id=t["id"], gallery_id=g["id"], filename="a.jpg",
              fileobj=io.BytesIO(b"x" * 8), content_type="image/jpeg")
    publish_gallery(conn, t["id"], g["id"])
    token = enable_delivery(conn, t["id"], g["id"])
    conn.commit()

    with caplog.at_level("INFO", logger="hestia.access"):
        TestClient(create_app(settings)).get(f"/d/{token}")
    logged_paths = [getattr(r, "path", "") for r in caplog.records]
    assert any(p == "/d/[redacted]" for p in logged_paths)      # route observability kept
    assert not any(token in (p or "") for p in logged_paths)    # credential never logged

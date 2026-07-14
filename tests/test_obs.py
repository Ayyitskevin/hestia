"""Structured logging — JSON formatter, request ids, access-log middleware."""

import json
import logging
import sys

from hestia.obs import JsonFormatter, configure_logging, new_request_id
from hestia.private_surfaces import PRIVATE_SURFACE_PREFIXES


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
    # Every registered private surface keeps its route prefix but drops the full tail.
    for prefix in PRIVATE_SURFACE_PREFIXES:
        assert redact_path(f"{prefix}credential/tail") == f"{prefix}[redacted]"
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


def test_access_log_never_persists_a_proposal_token(settings, conn, caplog):
    """Regression: proposal links are bearer credentials too, including real requests."""
    from starlette.testclient import TestClient

    from hestia.main import create_app
    from hestia.packages import create_package
    from hestia.proposals import create_proposal, send_proposal
    from hestia.tenants import create_tenant

    tenant = create_tenant(conn, name="Proposal Log Studio", shoot_type="wedding")
    package = create_package(
        conn, tenant_id=tenant["id"], name="Wedding Collection", price_cents=350000
    )
    proposal = create_proposal(
        conn,
        settings,
        tenant_id=tenant["id"],
        package_id=package["id"],
        title="Private proposal",
    )
    send_proposal(conn, tenant["id"], proposal["id"])
    conn.commit()

    with caplog.at_level("INFO", logger="hestia.access"):
        response = TestClient(create_app(settings)).get(f"/proposal/{proposal['token']}")

    assert response.status_code == 200
    logged_paths = [getattr(record, "path", "") for record in caplog.records]
    assert any(path == "/proposal/[redacted]" for path in logged_paths)
    assert not any(proposal["token"] in (path or "") for path in logged_paths)

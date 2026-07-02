"""Branded error pages — a mistyped URL or a rare crash meets the Hestia voice, not
raw framework JSON or a stack trace. API/webhook routes keep their JSON contract."""

from fastapi.testclient import TestClient

from hestia.main import create_app


def test_browser_404_is_a_warm_branded_page(client):
    r = client.get("/this-page-does-not-exist")
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Page not found" in r.text
    assert "Take me home" in r.text
    assert '<meta name="robots" content="noindex">' in r.text   # error pages stay unindexed


def test_api_404_stays_json(client):
    r = client.get("/api/nope")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"]                                   # machine-readable, not HTML


def test_unhandled_error_renders_a_friendly_500_without_leaking(settings):
    app = create_app(settings)

    @app.get("/_boom_test")
    def _boom():
        raise RuntimeError("kaboom-secret-internal")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/_boom_test")
    assert r.status_code == 500
    assert "sideways" in r.text.lower()                         # warm copy
    assert "kaboom-secret-internal" not in r.text              # never leak the exception


def test_api_500_stays_json(settings):
    app = create_app(settings)

    @app.get("/api/_boom_test")
    def _boom():
        raise RuntimeError("kaboom")

    c = TestClient(app, raise_server_exceptions=False)
    r = c.get("/api/_boom_test")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")

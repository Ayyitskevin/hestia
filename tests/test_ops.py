"""Operator surfaces — /readyz, the admin system view, queue + migration introspection."""

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.db import applied_migrations, get_db
from hestia.jobs import enqueue, queue_stats


def _admin(app):
    c = CSRFClient(app)
    c.post("/admin/login", data={"token": ADMIN_TOKEN})
    return c


def test_readyz_reports_ready(client):
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["checks"] == {"db": True, "migrations": True, "storage": True}


def test_applied_migrations_lists_the_ledger(conn):
    versions = [m["version"] for m in applied_migrations(conn)]
    assert versions == sorted(versions)            # oldest-first
    assert "0001" in versions and "0006" in versions  # baseline → subscriptions


def test_queue_stats_counts_by_status(db_path):
    with get_db(db_path) as conn:
        enqueue(conn, kind="ops.noop")
        enqueue(conn, kind="ops.noop")
    with get_db(db_path) as conn:
        stats = queue_stats(conn)
    assert stats == {"queued": 2, "running": 0, "done": 0, "error": 0}


def test_admin_system_requires_admin(client):
    r = client.get("/admin/system", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"


def test_admin_system_renders_for_admin(app):
    page = _admin(app).get("/admin/system")
    assert page.status_code == 200
    assert "System" in page.text
    assert "Job queue" in page.text and "Backends" in page.text
    assert "0001_baseline" in page.text  # migration ledger is surfaced

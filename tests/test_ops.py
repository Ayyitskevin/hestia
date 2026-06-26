"""Operator surfaces — /readyz, the admin system view, queue + migration introspection."""

from conftest import ADMIN_TOKEN, CSRFClient

from hestia.db import applied_migrations, get_db
from hestia.jobs import (
    HANDLERS,
    enqueue,
    failed_jobs,
    queue_stats,
    register,
    requeue_job,
    run_worker,
    stale_jobs,
)


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
    assert "fulfillment" in page.text    # every seam is listed, incl. the latest


# --- dead-letter introspection + requeue ------------------------------------

def test_failed_jobs_lists_only_dead_letter(db_path):
    with get_db(db_path) as conn:
        conn.execute("INSERT INTO jobs (kind, status, attempts, max_attempts, last_error, "
                     "finished_at) VALUES ('cull.boom','error',3,3,'kaboom',datetime('now'))")
        enqueue(conn, kind="ok.job")  # a healthy queued job is not dead-letter
    with get_db(db_path) as conn:
        failed = failed_jobs(conn)
    assert len(failed) == 1
    assert failed[0]["kind"] == "cull.boom" and failed[0]["last_error"] == "kaboom"


def test_stale_jobs_flags_orphans_not_fresh(db_path):
    with get_db(db_path) as conn:
        conn.execute("INSERT INTO jobs (kind, status, started_at) "
                     "VALUES ('stuck','running',datetime('now','-20 minutes'))")
        conn.execute("INSERT INTO jobs (kind, status, started_at) "
                     "VALUES ('fresh','running',datetime('now'))")
    with get_db(db_path) as conn:
        stale = stale_jobs(conn)
    assert [j["kind"] for j in stale] == ["stuck"]  # only the orphan, not the fresh claim


def test_requeue_job_is_idempotent_and_resets_run_at(db_path):
    with get_db(db_path) as conn:
        jid = conn.execute(
            "INSERT INTO jobs (kind, status, attempts, max_attempts, run_at) "
            "VALUES ('boom','error',3,3,datetime('now','+99 years'))").lastrowid
        done = conn.execute("INSERT INTO jobs (kind, status) VALUES ('d','done')").lastrowid
    with get_db(db_path) as conn:
        assert requeue_job(conn, jid) is True               # error -> queued
    with get_db(db_path) as conn:
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
        assert row["status"] == "queued"
        runnable = conn.execute(                            # run_at was reset to now
            "SELECT 1 FROM jobs WHERE id=? AND run_at <= datetime('now')", (jid,)).fetchone()
        assert runnable is not None
        assert requeue_job(conn, jid) is False              # already queued -> no-op
        assert requeue_job(conn, done) is False             # done -> no-op
        assert requeue_job(conn, 10_000_000) is False       # missing -> no-op


def test_admin_system_shows_dead_letter(app):
    with get_db(app.state.settings.db_path) as conn:
        conn.execute("INSERT INTO jobs (kind, status, attempts, max_attempts, last_error, "
                     "finished_at) VALUES ('pipeline.run','error',3,3,'provider 500',datetime('now'))")
    page = _admin(app).get("/admin/system")
    assert "Dead-letter queue" in page.text
    assert "pipeline.run" in page.text and "provider 500" in page.text
    assert "/admin/system/jobs/" in page.text and "Requeue" in page.text


def test_admin_system_requeue_action(app):
    with get_db(app.state.settings.db_path) as conn:
        jid = conn.execute("INSERT INTO jobs (kind, status, attempts, max_attempts) "
                           "VALUES ('boom','error',3,3)").lastrowid
    r = _admin(app).post(f"/admin/system/jobs/{jid}/requeue", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/system"
    with get_db(app.state.settings.db_path) as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == "queued"


def test_requeue_requires_admin(app):
    with get_db(app.state.settings.db_path) as conn:
        jid = conn.execute("INSERT INTO jobs (kind, status, attempts, max_attempts) "
                           "VALUES ('boom','error',3,3)").lastrowid
    # anonymous (no session) → the route's admin gate redirects; the job is untouched
    r = CSRFClient(app).post(f"/admin/system/jobs/{jid}/requeue", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    with get_db(app.state.settings.db_path) as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()["status"] == "error"


def test_admin_surface_is_navigable(app):
    # the operator landing page renders the nav, so System is one click away
    # (admin pages used to render with auth=None, hiding every nav link)
    page = _admin(app).get("/admin/tenants")
    assert 'href="/admin/system"' in page.text


def test_admin_system_flags_config_warnings(settings):
    # a real backend selected without its credentials must be shouted about
    import dataclasses

    from hestia.main import create_app

    bad = dataclasses.replace(settings, payments_backend="stripe", stripe_secret_key="")
    admin = CSRFClient(create_app(bad))
    admin.post("/admin/login", data={"token": ADMIN_TOKEN})
    page = admin.get("/admin/system")
    assert "configuration warning" in page.text.lower()
    assert "HESTIA_STRIPE_SECRET_KEY" in page.text


# --- hardening: requeue only orphaned 'running' jobs, reclaim on a cadence ----

def test_requeue_running_only_when_stale(db_path):
    with get_db(db_path) as conn:
        fresh = conn.execute("INSERT INTO jobs (kind, status, started_at) "
                             "VALUES ('live','running',datetime('now'))").lastrowid
        orphan = conn.execute("INSERT INTO jobs (kind, status, started_at) "
                             "VALUES ('orphan','running',datetime('now','-20 minutes'))").lastrowid
    with get_db(db_path) as conn:
        # a genuinely in-flight job must NOT be requeued (would double-run concurrently)
        assert requeue_job(conn, fresh) is False
        # a stale orphan is fair game
        assert requeue_job(conn, orphan) is True
    with get_db(db_path) as conn:
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (fresh,)).fetchone()["status"] == "running"
        assert conn.execute("SELECT status FROM jobs WHERE id=?", (orphan,)).fetchone()["status"] == "queued"


def test_worker_reclaims_orphaned_jobs_on_a_cadence(db_path, settings):
    import threading

    ran = threading.Event()
    HANDLERS.pop("test.orphan", None)

    @register("test.orphan")
    def _handle(settings, payload):  # noqa: ARG001
        ran.set()

    try:
        with get_db(db_path) as conn:
            # an orphan stuck in 'running' past the stale window (worker died mid-job)
            conn.execute("INSERT INTO jobs (kind, status, started_at, max_attempts) "
                         "VALUES ('test.orphan','running',datetime('now','-20 minutes'),3)")
        stop = threading.Event()
        worker = threading.Thread(
            target=run_worker, args=(db_path, settings, stop),
            kwargs={"idle_sleep": 0.01, "reclaim_interval": 0.0}, daemon=True)
        worker.start()
        try:
            # the job can only run once the loop reclaims it from 'running' → 'queued',
            # so the handler firing proves the periodic reclaim works (not just startup)
            assert ran.wait(timeout=3.0), "orphaned job was never reclaimed and re-run"
        finally:
            stop.set()
            worker.join(timeout=2)
    finally:
        HANDLERS.pop("test.orphan", None)

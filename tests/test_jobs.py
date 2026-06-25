"""Durable job queue — enqueue, atomic claim, retry/backoff, reclaim, dispatch."""

from conftest import login_owner, onboard_studio

from hestia.db import get_db
from hestia.jobs import (
    claim_next,
    drain,
    enqueue,
    list_jobs,
    reclaim_stale,
    register,
    run_next,
)

# Test handlers, registered once at import. Unique kinds avoid colliding with
# the real "pipeline.run" handler or each other; module state tracks invocations.
_calls = {"ok": 0, "flaky": 0, "boom": 0}


@register("test.ok")
def _ok(settings, payload):
    _calls["ok"] += 1


@register("test.flaky")
def _flaky(settings, payload):
    _calls["flaky"] += 1
    if _calls["flaky"] < 2:
        raise RuntimeError("first attempt fails")


@register("test.boom")
def _boom(settings, payload):
    _calls["boom"] += 1
    raise RuntimeError("always fails")


def _enq(db_path, **kw):
    with get_db(db_path) as conn:
        return enqueue(conn, **kw)


def _job(db_path, jid):
    with get_db(db_path) as conn:
        return dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())


def test_enqueue_and_atomic_claim(db_path):
    jid = _enq(db_path, kind="test.ok", payload={"x": 1}, tenant_id="t1")
    job = claim_next(db_path)
    assert job["id"] == jid and job["status"] == "running" and job["attempts"] == 1
    assert claim_next(db_path) is None  # already claimed → nothing left


def test_run_next_dispatches_and_completes(db_path, settings):
    before = _calls["ok"]
    jid = _enq(db_path, kind="test.ok")
    assert run_next(db_path, settings) == "test.ok"
    assert _calls["ok"] == before + 1
    assert _job(db_path, jid)["status"] == "done"


def test_unhandled_kind_errors_out(db_path, settings):
    jid = _enq(db_path, kind="test.nonexistent", max_attempts=1)
    run_next(db_path, settings)
    job = _job(db_path, jid)
    assert job["status"] == "error" and "no handler" in job["last_error"]


def test_failure_retries_with_backoff_then_errors(db_path, settings):
    jid = _enq(db_path, kind="test.boom", max_attempts=2)
    run_next(db_path, settings)                       # attempt 1 → requeued with backoff
    job = _job(db_path, jid)
    assert job["status"] == "queued" and job["attempts"] == 1 and job["last_error"]
    assert claim_next(db_path) is None                # backoff → not yet runnable

    with get_db(db_path) as conn:                     # make it runnable again
        conn.execute("UPDATE jobs SET run_at = datetime('now','-1 second') WHERE id=?", (jid,))
    run_next(db_path, settings)                       # attempt 2 → exhausted → error
    job = _job(db_path, jid)
    assert job["status"] == "error" and job["attempts"] == 2


def test_flaky_job_succeeds_on_retry(db_path, settings):
    _calls["flaky"] = 0
    jid = _enq(db_path, kind="test.flaky", max_attempts=3)
    run_next(db_path, settings)                       # fails once
    assert _job(db_path, jid)["status"] == "queued"
    with get_db(db_path) as conn:
        conn.execute("UPDATE jobs SET run_at = datetime('now','-1 second') WHERE id=?", (jid,))
    run_next(db_path, settings)                       # succeeds on the retry
    assert _job(db_path, jid)["status"] == "done"


def test_drain_runs_all_ready_jobs(db_path, settings):
    for _ in range(3):
        _enq(db_path, kind="test.ok")
    assert drain(db_path, settings) >= 3
    with get_db(db_path) as conn:
        pending = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'").fetchone()["n"]
    assert pending == 0


def test_reclaim_stale_requeues_only_orphans(db_path):
    stale = _enq(db_path, kind="test.ok")
    fresh = _enq(db_path, kind="test.ok")
    with get_db(db_path) as conn:
        conn.execute("UPDATE jobs SET status='running', started_at=datetime('now','-1 hour') WHERE id=?", (stale,))
        conn.execute("UPDATE jobs SET status='running', started_at=datetime('now') WHERE id=?", (fresh,))
    assert reclaim_stale(db_path, older_than_seconds=900) == 1
    assert _job(db_path, stale)["status"] == "queued"
    assert _job(db_path, fresh)["status"] == "running"  # recent job left alone


def test_list_jobs_is_tenant_scoped(db_path):
    _enq(db_path, kind="test.ok", tenant_id="ta")
    _enq(db_path, kind="test.ok", tenant_id="tb")
    with get_db(db_path) as conn:
        assert len(list_jobs(conn, "ta")) == 1


def test_process_route_runs_through_the_queue(client, conn):
    # /process enqueues a pipeline.run job; the request BackgroundTask drains it.
    login_owner(client, onboard_studio(client, email="queue@e.com"))
    gid = client.post("/galleries", data={"title": "Q"}).url.path.rstrip("/").split("/")[-1]
    client.post(f"/galleries/{gid}/images", files=[("files", ("a.jpg", b"x" * 32, "image/jpeg"))])
    client.post(f"/galleries/{gid}/process")
    job = conn.execute(
        "SELECT * FROM jobs WHERE kind='pipeline.run' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert job is not None and job["status"] == "done"

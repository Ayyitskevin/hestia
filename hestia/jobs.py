"""Durable background job queue (SQLite-backed).

Replaces fire-and-forget ``BackgroundTasks`` for work that must survive a restart.
A job is a row; it's claimed atomically, run by a registered handler, and retried
with exponential backoff on failure. Two things drain the queue:

- a **worker thread** started in the app lifespan (the durable backstop — picks up
  retries and jobs orphaned by a crash, via :func:`reclaim_stale`), and
- an inline :func:`drain` kicked off as a request ``BackgroundTask`` right after a
  job is enqueued, so work starts immediately and tests stay synchronous.

Both call the same :func:`run_next`; the claim is atomic, so a job never double-runs.
Handlers register with ``@register("kind")`` and receive ``(settings, payload)``.
All timestamps use SQLite's ``datetime('now')`` so comparisons stay consistent.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .db import get_db

# kind -> handler(settings, payload)
HANDLERS: dict[str, Callable] = {}

MAX_BACKOFF_SECONDS = 300


def register(kind: str):
    """Decorator: register a handler for a job kind."""
    def deco(fn: Callable) -> Callable:
        HANDLERS[kind] = fn
        return fn
    return deco


def enqueue(conn, *, kind: str, payload: dict | None = None, tenant_id: str | None = None,
            max_attempts: int = 3, run_at: str | None = None) -> int:
    """Enqueue a job. ``run_at`` (a SQLite datetime string) delays the earliest
    run — used for scheduled work like reminders; omit it to run as soon as a
    worker is free."""
    if run_at is None:
        cur = conn.execute(
            "INSERT INTO jobs (tenant_id, kind, payload_json, max_attempts) VALUES (?, ?, ?, ?)",
            (tenant_id, kind, json.dumps(payload or {}), max_attempts),
        )
    else:
        cur = conn.execute(
            "INSERT INTO jobs (tenant_id, kind, payload_json, max_attempts, run_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, kind, json.dumps(payload or {}), max_attempts, run_at),
        )
    return cur.lastrowid


def claim_next(db_path: str | Path) -> dict | None:
    """Atomically claim the oldest runnable queued job (or None)."""
    with get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM jobs WHERE status='queued' AND run_at <= datetime('now') "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE jobs SET status='running', attempts=attempts+1, "
            "started_at=datetime('now'), updated_at=datetime('now') "
            "WHERE id = ? AND status='queued'",
            (row["id"],),
        )
        if cur.rowcount == 0:  # lost the race to another drainer
            return None
        return dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone())


def _complete(db_path: str | Path, job_id: int) -> None:
    with get_db(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=datetime('now'), "
            "updated_at=datetime('now') WHERE id=?",
            (job_id,),
        )


def _fail(db_path: str | Path, job: dict, error: str) -> None:
    with get_db(db_path) as conn:
        if job["attempts"] >= job["max_attempts"]:
            conn.execute(
                "UPDATE jobs SET status='error', last_error=?, finished_at=datetime('now'), "
                "updated_at=datetime('now') WHERE id=?",
                (error[:1000], job["id"]),
            )
        else:
            backoff = min(2 ** job["attempts"], MAX_BACKOFF_SECONDS)
            conn.execute(
                "UPDATE jobs SET status='queued', last_error=?, run_at=datetime('now', ?), "
                "updated_at=datetime('now') WHERE id=?",
                (error[:1000], f"+{backoff} seconds", job["id"]),
            )


def run_next(db_path: str | Path, settings: Settings) -> str | None:
    """Claim and run one job. Returns the kind run, or None if nothing was runnable."""
    job = claim_next(db_path)
    if not job:
        return None
    handler = HANDLERS.get(job["kind"])
    try:
        if handler is None:
            raise RuntimeError(f"no handler registered for job kind '{job['kind']}'")
        handler(settings, json.loads(job["payload_json"] or "{}"))
    except Exception as exc:  # noqa: BLE001 - failures are recorded + retried, never raised out
        _fail(db_path, job, str(exc))
        return job["kind"]
    _complete(db_path, job["id"])
    return job["kind"]


def drain(db_path: str | Path, settings: Settings, *, max_jobs: int = 100) -> int:
    """Run runnable jobs until the queue is empty (or max_jobs). Returns count run."""
    n = 0
    while n < max_jobs and run_next(db_path, settings) is not None:
        n += 1
    return n


def reclaim_stale(db_path: str | Path, *, older_than_seconds: int = 900) -> int:
    """Re-queue jobs stuck in 'running' (a worker died mid-job). Returns count reclaimed."""
    with get_db(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='queued', updated_at=datetime('now') "
            "WHERE status='running' AND started_at < datetime('now', ?)",
            (f"-{older_than_seconds} seconds",),
        )
        return cur.rowcount


def list_jobs(conn, tenant_id: str, *, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE tenant_id = ? ORDER BY id DESC LIMIT ?", (tenant_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def queue_stats(conn) -> dict:
    """Job counts by status (for the operator/system view)."""
    stats = {"queued": 0, "running": 0, "done": 0, "error": 0}
    for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        stats[r["status"]] = r["n"]
    return stats


def failed_jobs(conn, *, limit: int = 50) -> list[dict]:
    """The dead-letter queue: jobs that exhausted their retries (status='error'),
    most-recent first. These never run again on their own — an operator requeues
    them once the underlying cause is fixed."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='error' ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def stale_jobs(conn, *, older_than_seconds: int = 900, limit: int = 50) -> list[dict]:
    """Jobs stuck in 'running' past the reclaim window — a worker most likely died
    mid-job. :func:`run_worker` reclaims these on its next loop; surfacing them lets
    an operator see the lag (or force a requeue) instead of waiting."""
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='running' AND started_at < datetime('now', ?) "
        "ORDER BY id DESC LIMIT ?",
        (f"-{older_than_seconds} seconds", limit),
    ).fetchall()
    return [dict(r) for r in rows]


def requeue_job(conn, job_id: int, *, stale_after_seconds: int = 900) -> bool:
    """Operator action: send a failed or *orphaned* job back to the queue to run now.

    An 'error' job (dead-letter) requeues unconditionally. A 'running' job requeues
    only if it's actually stale (``started_at`` past the reclaim window) — requeuing
    a job that's genuinely still in flight would flip it to 'queued' and let another
    worker claim and run it concurrently (e.g. a duplicate email/charge). A 'queued'
    or 'done' row, or a fresh 'running' one, is left alone, so a double-click or a
    stale page is a no-op. It does *not* reset the attempt budget. Returns True iff a
    row changed."""
    cur = conn.execute(
        "UPDATE jobs SET status='queued', run_at=datetime('now'), finished_at=NULL, "
        "updated_at=datetime('now') WHERE id = ? AND ("
        "    status='error' "
        "    OR (status='running' AND started_at < datetime('now', ?)))",
        (job_id, f"-{stale_after_seconds} seconds"),
    )
    return cur.rowcount > 0


def run_worker(db_path: str | Path, settings: Settings, stop_event, *, idle_sleep: float = 0.5,
               reclaim_interval: float = 60.0) -> None:
    """Background loop: drain the queue and periodically reclaim orphaned jobs.

    Reclaiming must run *on a cadence*, not just at startup: a job orphaned by a
    restart is younger than the stale window at that moment, so a single startup
    sweep skips it and nothing would ever pick it up once it ages past the window.
    We reclaim every ``reclaim_interval`` seconds so an orphan is recovered within
    roughly ``stale_window + reclaim_interval``."""
    last_reclaim = 0.0  # 0 → reclaim immediately on the first iteration
    while not stop_event.is_set():
        if time.monotonic() - last_reclaim >= reclaim_interval:
            try:
                reclaim_stale(db_path)
            except Exception:  # noqa: BLE001 - never let reclaim kill the worker
                pass
            last_reclaim = time.monotonic()
        try:
            kind = run_next(db_path, settings)
        except Exception:  # noqa: BLE001 - the worker must never die on a bad job
            kind = None
        if kind is None:
            stop_event.wait(idle_sleep)

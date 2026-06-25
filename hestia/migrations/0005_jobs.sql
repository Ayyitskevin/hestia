-- 0005_jobs — a durable, SQLite-backed job queue. Replaces fire-and-forget
-- BackgroundTasks for work that must survive a restart: jobs are rows, claimed
-- atomically, run by a registered handler, and retried with backoff on failure.

CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'queued',   -- queued | running | done | error
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    run_at       TEXT NOT NULL DEFAULT (datetime('now')),  -- earliest time to run (backoff)
    last_error   TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_jobs_tenant ON jobs(tenant_id, id DESC);

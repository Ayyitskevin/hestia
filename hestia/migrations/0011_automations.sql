-- 0011_automations — the event-triggered workflow engine.
--
-- A studio defines rules: "when <event> happens, email the client this." When a
-- domain event fires (contract signed, invoice paid, questionnaire completed,
-- project booked, gallery published), the emitting code enqueues a durable job
-- per matching enabled rule; the worker renders the template and sends via the
-- email seam. Emission is conn-only (cheap, inside the triggering transaction);
-- execution is on the job queue (retried, off the request thread). Every run is
-- recorded in automation_runs for observability — an automation can't silently
-- misfire.

CREATE TABLE IF NOT EXISTS automations (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    trigger    TEXT NOT NULL,                       -- event key (see automations.TRIGGERS)
    action     TEXT NOT NULL DEFAULT 'email_client',
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS automation_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    automation_id INTEGER REFERENCES automations(id) ON DELETE SET NULL,
    trigger       TEXT NOT NULL,
    status        TEXT NOT NULL,                    -- sent | skipped | failed
    detail        TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_automations_match ON automations(tenant_id, trigger, enabled);
CREATE INDEX IF NOT EXISTS idx_automation_runs_tenant ON automation_runs(tenant_id, created_at);

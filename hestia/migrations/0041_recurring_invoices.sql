-- 0041_recurring_invoices — retainer / subscription-style billing on a cadence.
-- A profile is an invoice template (title, amount, client, note) plus a cadence and the
-- date the next invoice is due to generate. A periodic worker sweep generates the next
-- invoice when next_run_at <= today and advances next_run_at by one period — atomically,
-- so a double sweep never double-bills. Each generated invoice is an ordinary invoice
-- (own pay link, same idempotent settle path); the profile just spawns them. Money is
-- integer cents; tenant-scoped, cascade-deleted with the tenant.

CREATE TABLE IF NOT EXISTS recurring_invoices (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title            TEXT NOT NULL DEFAULT '',
    amount_cents     INTEGER NOT NULL DEFAULT 0,
    client_id        INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    note             TEXT NOT NULL DEFAULT '',
    cadence          TEXT NOT NULL DEFAULT 'monthly',   -- weekly | monthly | yearly
    next_run_at      TEXT NOT NULL,                     -- date (YYYY-MM-DD) the next invoice generates
    active           INTEGER NOT NULL DEFAULT 1,
    last_invoiced_at TEXT,
    invoice_count    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_recurring_due ON recurring_invoices(active, next_run_at);

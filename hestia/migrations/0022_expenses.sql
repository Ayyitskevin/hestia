-- 0022_expenses — the Plutus side of the ledger. Hestia records revenue (paid
-- invoices + orders); this adds expenses so a studio sees real profit, per shoot
-- and overall. Expenses optionally tag a project for per-job P&L.

CREATE TABLE IF NOT EXISTS expenses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id   INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    category     TEXT NOT NULL DEFAULT 'other',
    description  TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    incurred_on  TEXT NOT NULL DEFAULT '',            -- free-text date the owner enters
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_expenses_tenant ON expenses(tenant_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_expenses_project ON expenses(tenant_id, project_id);

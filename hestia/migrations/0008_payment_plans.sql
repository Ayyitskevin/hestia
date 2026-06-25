-- 0008_payment_plans — split a booking total into scheduled installments
-- (the classic "deposit to book, balance by the event date" flow, and any
-- N-installment plan generally).
--
-- An installment IS an invoice: each one is a payable row with its own public
-- pay link and the same idempotent settle path, so a plan reuses the money
-- spine rather than inventing a second one. The payment_plans row groups the
-- installments and carries the agreed total; per-plan progress (paid / partial
-- / open) is derived from the child invoices, never stored, so it can't drift
-- from what was actually collected.

CREATE TABLE IF NOT EXISTS payment_plans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id   INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    project_id  INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title       TEXT NOT NULL,
    total_cents INTEGER NOT NULL DEFAULT 0,
    currency    TEXT NOT NULL DEFAULT 'usd',
    status      TEXT NOT NULL DEFAULT 'active',   -- active | void
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Installments hang off invoices: an invoice in a plan carries the plan id, its
-- order in the schedule, and its due date. A nullable FK (default NULL) is the
-- only ADD COLUMN form SQLite allows, which is exactly what we want.
ALTER TABLE invoices ADD COLUMN plan_id INTEGER REFERENCES payment_plans(id) ON DELETE SET NULL;
ALTER TABLE invoices ADD COLUMN due_date TEXT NOT NULL DEFAULT '';
ALTER TABLE invoices ADD COLUMN sequence INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_payment_plans_tenant ON payment_plans(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_payment_plans_project ON payment_plans(project_id);
CREATE INDEX IF NOT EXISTS idx_invoices_plan ON invoices(plan_id, sequence);

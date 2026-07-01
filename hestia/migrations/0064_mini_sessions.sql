-- 0064_mini_sessions -- limited booking drops for profitable mini-session days.
-- A mini-session drop is a published set of fixed slots. Claiming one slot reuses
-- the normal booking/client/project/appointment/invoice path, while the slot row
-- keeps the drop-specific public availability state.

CREATE TABLE IF NOT EXISTS mini_sessions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    slug             TEXT NOT NULL,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    duration_minutes INTEGER NOT NULL DEFAULT 20,
    price_cents      INTEGER NOT NULL DEFAULT 0,
    deposit_cents    INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'draft',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, slug)
);

CREATE INDEX IF NOT EXISTS idx_mini_sessions_tenant_status
    ON mini_sessions(tenant_id, status, created_at);

CREATE TABLE IF NOT EXISTS mini_session_slots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    mini_session_id INTEGER NOT NULL REFERENCES mini_sessions(id) ON DELETE CASCADE,
    starts_at       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    client_id       INTEGER,
    project_id      INTEGER,
    appointment_id  INTEGER,
    invoice_id      INTEGER,
    claimed_at      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, mini_session_id, starts_at)
);

CREATE INDEX IF NOT EXISTS idx_mini_session_slots_drop_status
    ON mini_session_slots(tenant_id, mini_session_id, status, starts_at);

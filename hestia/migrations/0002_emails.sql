-- 0002_emails — the transactional-email outbox. Every notification Hestia sends
-- (or, in mock mode, would send) is recorded here, so nothing silently vanishes
-- and the owner can audit it at /settings/outbox.

CREATE TABLE IF NOT EXISTS emails (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    to_addr    TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    backend    TEXT NOT NULL DEFAULT 'mock',
    status     TEXT NOT NULL DEFAULT 'recorded',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_emails_tenant ON emails(tenant_id, created_at DESC);

-- 0026_client_tags — segment clients with free-form tags (VIP, 2026-wedding,
-- repeat). A client can carry many tags; a tag is unique per client. Used to filter
-- the clients list and group the book of business.

CREATE TABLE IF NOT EXISTS client_tags (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id  INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    tag        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, client_id, tag)
);

CREATE INDEX IF NOT EXISTS idx_client_tags_tenant ON client_tags(tenant_id, tag);
CREATE INDEX IF NOT EXISTS idx_client_tags_client ON client_tags(tenant_id, client_id);

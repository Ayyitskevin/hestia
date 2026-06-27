-- 0032_message_templates — let a studio customize the copy of the client emails
-- Hestia sends on its behalf (booking confirmation, reminder, invoice). A row
-- overrides the built-in default subject/body for one kind; absent → the default is
-- used, so existing studios' mail is unchanged until they edit a template. One
-- override per (tenant, kind). The studio's signature is still appended by the mailer.

CREATE TABLE IF NOT EXISTS message_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, kind)
);

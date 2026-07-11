-- 0067_ai_usage — ledger of live AI provider calls per studio.
--
-- One row per recorded usage event (vision frame, album arrange, content pack, …).
-- Mock backends never write here. Used for founder cost control during beta.

CREATE TABLE IF NOT EXISTS ai_usage_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gallery_id  INTEGER,
    module      TEXT NOT NULL,
    backend     TEXT NOT NULL DEFAULT 'mock',
    units       INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ai_usage_tenant ON ai_usage_events(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_ai_usage_gallery ON ai_usage_events(gallery_id, created_at);

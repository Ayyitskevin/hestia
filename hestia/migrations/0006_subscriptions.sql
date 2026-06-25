-- 0006_subscriptions — one subscription row per studio, tracking the active plan
-- and (for stripe) the provider reference. The authoritative plan stays on
-- tenants.plan; this row carries status/provider/history for billing.

CREATE TABLE IF NOT EXISTS subscriptions (
    tenant_id    TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    plan         TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',    -- active | canceled | past_due
    provider     TEXT NOT NULL DEFAULT 'mock',
    provider_ref TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

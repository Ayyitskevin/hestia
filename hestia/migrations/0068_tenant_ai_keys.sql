-- 0068_tenant_ai_keys — per-studio xAI API key (bring-your-own, post-subsidy).
--
-- The founder-hosted beta subsidy covers the first gallery per studio. Once it's
-- used, a studio can keep live AI vision by supplying their own xAI key, which
-- bypasses the subsidy gallery cap and image cap (they pay xAI directly).
--
-- The key is stored Fernet-encrypted at rest (see hestia/crypto.py); only the
-- boolean ``has_key`` is readable without decrypting, so "is a key configured?"
-- never needs to touch the cipher. One row per tenant (upsert on set).

CREATE TABLE IF NOT EXISTS tenant_ai_keys (
    tenant_id   TEXT PRIMARY KEY REFERENCES tenants(id) ON DELETE CASCADE,
    key_enc     TEXT NOT NULL DEFAULT '',
    has_key     INTEGER NOT NULL DEFAULT 0,
    set_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

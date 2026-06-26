-- 0021_referral_rewards — close the word-of-mouth loop: when a referred lead books,
-- the referring client earns a credit. One credit per converted project (UNIQUE),
-- so re-booking never double-credits. Redemption is manual (the owner applies it).

CREATE TABLE IF NOT EXISTS referral_credits (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id    INTEGER NOT NULL,                 -- the referrer earning the credit
    project_id   INTEGER NOT NULL,                 -- the converted referred project
    amount_cents INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'earned',   -- earned | redeemed
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    redeemed_at  TEXT,
    UNIQUE (project_id)
);

CREATE INDEX IF NOT EXISTS idx_referral_credits_client ON referral_credits(tenant_id, client_id, status);

-- The studio's flat reward per successful referral (default $50). Configurable later.
ALTER TABLE tenants ADD COLUMN referral_reward_cents INTEGER NOT NULL DEFAULT 5000;

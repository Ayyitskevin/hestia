-- 0063_beta_interest_invites -- invite-only beta interest conversion tracking.

ALTER TABLE beta_interests ADD COLUMN invite_token_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE beta_interests ADD COLUMN invited_at TEXT;
ALTER TABLE beta_interests ADD COLUMN invite_expires_at TEXT;
ALTER TABLE beta_interests ADD COLUMN invite_email_status TEXT NOT NULL DEFAULT '';
ALTER TABLE beta_interests ADD COLUMN tenant_id TEXT NOT NULL DEFAULT '';
ALTER TABLE beta_interests ADD COLUMN converted_at TEXT;

CREATE INDEX IF NOT EXISTS idx_beta_interests_invite_token
    ON beta_interests(invite_token_hash);

CREATE INDEX IF NOT EXISTS idx_beta_interests_tenant
    ON beta_interests(tenant_id);

CREATE INDEX IF NOT EXISTS idx_beta_interests_status
    ON beta_interests(status, updated_at DESC);

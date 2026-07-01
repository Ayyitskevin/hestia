-- 0061_signup_attribution -- first-party hosted acquisition source.
-- Operators need to see whether new studios came through the public pricing page,
-- demo tour, or direct/unknown signup path without storing arbitrary referrers.

ALTER TABLE tenants ADD COLUMN signup_source TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN signup_landing_path TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_tenants_signup_source
    ON tenants(signup_source, created_at DESC);

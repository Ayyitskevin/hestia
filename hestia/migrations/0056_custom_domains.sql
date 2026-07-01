-- 0056_custom_domains — hosted studios can prepare a branded domain.
-- The owner stores the desired host and gets a stable DNS verification token.
-- A domain only routes publicly after an operator/verifier marks it verified.

ALTER TABLE tenants ADD COLUMN custom_domain TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN custom_domain_status TEXT NOT NULL DEFAULT 'unset';
ALTER TABLE tenants ADD COLUMN custom_domain_token TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN custom_domain_updated_at TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_custom_domain_unique
    ON tenants(custom_domain)
    WHERE custom_domain <> '';

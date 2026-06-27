-- 0037_tenant_digest_enabled — per-studio on/off switch for the weekly owner digest.
-- Defaults on (1) so existing studios keep getting it; an owner who finds it noisy can
-- turn it off in settings. The digest sweep skips disabled studios; the manual "email me
-- this" button still works regardless (the owner asked for it explicitly).

ALTER TABLE tenants ADD COLUMN digest_enabled INTEGER NOT NULL DEFAULT 1;

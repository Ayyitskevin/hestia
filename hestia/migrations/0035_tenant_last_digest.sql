-- 0035_tenant_last_digest — remember when each studio last got its owner digest (the
-- weekly "here's what needs you" summary email). NULL means never sent. The digest
-- sweep claims this timestamp before sending (gated on a cooldown), so a studio is
-- emailed at most once per cooldown window even if the worker runs hourly — the same
-- claim-before-send pattern as the per-document reminder cooldowns.

ALTER TABLE tenants ADD COLUMN last_digest_at TEXT;

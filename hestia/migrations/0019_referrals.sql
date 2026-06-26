-- 0019_referrals — word-of-mouth attribution. Each client gets an unguessable
-- referral code; an inquiry that arrives through /studio/{slug}?ref={code} is
-- tagged back to the referring client, so the studio can see which past clients
-- drive new business. Pure attribution — no rewards engine, no new write surface.

ALTER TABLE clients ADD COLUMN referral_code TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN referred_by_client_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_clients_referral ON clients(tenant_id, referral_code);

-- 0009_client_portal — a per-client, unguessable portal link.
--
-- Each client can have one long-lived portal token: a single branded URL where
-- they see their projects, contracts to sign, payment schedule, and galleries in
-- one place. Same unguessable-token model as offers / pay / sign links — no
-- client passwords. The token is nullable (portals are opt-in per client) and
-- rotatable (regenerating revokes the old link). A partial unique index keeps
-- non-null tokens unique while allowing many clients to have none.

ALTER TABLE clients ADD COLUMN portal_token TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_portal_token
    ON clients(portal_token) WHERE portal_token IS NOT NULL;

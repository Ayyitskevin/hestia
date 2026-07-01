-- 0058_proposal_reminders -- proposal follow-up bookkeeping.
-- A proposal can stall before acceptance, or after acceptance while the client
-- still needs to sign/pay. These fields let owners nudge from Hestia and see
-- whether a client has already been reminded.

ALTER TABLE proposals ADD COLUMN last_reminder_at TEXT;
ALTER TABLE proposals ADD COLUMN reminder_count   INTEGER NOT NULL DEFAULT 0;

-- 0060_proposal_views -- lightweight proposal engagement.
-- Owners need to know whether a stalled proposal has not been opened yet or was
-- viewed and still not accepted. Keep this deliberately simple: total views and
-- the latest viewed timestamp.

ALTER TABLE proposals ADD COLUMN view_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE proposals ADD COLUMN last_viewed_at TEXT;

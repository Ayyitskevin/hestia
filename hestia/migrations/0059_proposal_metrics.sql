-- 0059_proposal_metrics -- track when a proposal first went live.
-- updated_at changes on acceptance/reminders, so proposal conversion analytics need
-- a stable sent_at timestamp for sent -> accepted -> paid timing.

ALTER TABLE proposals ADD COLUMN sent_at TEXT;

UPDATE proposals
   SET sent_at = COALESCE(sent_at, updated_at, created_at)
 WHERE status IN ('sent', 'accepted')
   AND sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_proposals_tenant_sent_at
    ON proposals (tenant_id, sent_at);

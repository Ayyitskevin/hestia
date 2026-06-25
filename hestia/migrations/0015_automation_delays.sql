-- 0015_automation_delays — scheduled (retention) automations.
--
-- Generalizes the workflow engine: an automation can now fire a set number of
-- days AFTER its trigger, not just immediately. The delay rides the job queue's
-- run_at (emit enqueues the job with run_at = now + delay_days), so a rule like
-- "1 year after a gallery is delivered, email a re-book offer" just sits durably
-- in the queue until its time comes. delay_days = 0 keeps the original
-- fire-immediately behavior, so every existing rule is unchanged.

ALTER TABLE automations ADD COLUMN delay_days INTEGER NOT NULL DEFAULT 0;

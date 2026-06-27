-- 0029_document_reminders — chase unsigned contracts and unfilled questionnaires,
-- the same way overdue invoices are chased (0023). A contract/questionnaire still
-- 'sent' (not yet signed/completed) gets an auto-nudge on a cooldown; these columns
-- keep it idempotent — at most one reminder per cooldown window — and track how many
-- nudges have gone out. Defaults leave every existing row un-reminded.

ALTER TABLE contracts      ADD COLUMN last_reminder_at TEXT;             -- null until first reminder
ALTER TABLE contracts      ADD COLUMN reminder_count   INTEGER NOT NULL DEFAULT 0;
ALTER TABLE questionnaires ADD COLUMN last_reminder_at TEXT;
ALTER TABLE questionnaires ADD COLUMN reminder_count   INTEGER NOT NULL DEFAULT 0;

-- 0023_invoice_reminders — chase late invoices. An invoice that's still 'sent'
-- past its due_date is overdue; the worker nudges the client on a cadence and the
-- owner can also remind by hand. These columns keep the auto-reminder idempotent:
-- we re-send at most once per cooldown window, and track how many times we've
-- nudged so it can show on the invoice.

ALTER TABLE invoices ADD COLUMN last_reminder_at TEXT;             -- null until first reminder
ALTER TABLE invoices ADD COLUMN reminder_count   INTEGER NOT NULL DEFAULT 0;

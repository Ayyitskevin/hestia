-- 0028_invoice_note — let a studio add a personal message to an invoice (a thank-you,
-- payment instructions like "Venmo @studio also accepted", deposit terms). It's shown
-- on the client's pay page and carried in the send email. Display only — it never
-- touches amount_cents/tax_cents/totals. Default '' → existing invoices unchanged.

ALTER TABLE invoices ADD COLUMN note TEXT NOT NULL DEFAULT '';

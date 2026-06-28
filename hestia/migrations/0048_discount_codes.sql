-- 0048_discount_codes — studio promo codes applied at checkout.
-- A studio creates reusable codes (percent-off or a fixed amount off), optionally usage-
-- limited and/or expiring. A client paying an invoice can enter a code to reduce what they
-- pay: the discount comes off the invoice subtotal, tax scales proportionally, and the
-- charge/receipt/A-R/statement all follow from the (now-reduced) amount_cents/tax_cents —
-- so no money figure has to be recomputed anywhere else. Codes are unique per studio,
-- soft-disabled (active = 0) rather than deleted. Integer cents; tenant-scoped.

CREATE TABLE IF NOT EXISTS discount_codes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code        TEXT NOT NULL,                       -- normalized upper-case
    kind        TEXT NOT NULL DEFAULT 'percent',     -- 'percent' | 'fixed'
    value       INTEGER NOT NULL DEFAULT 0,          -- percent: 1..100; fixed: cents off
    active      INTEGER NOT NULL DEFAULT 1,
    max_uses    INTEGER NOT NULL DEFAULT 0,          -- 0 = unlimited
    used_count  INTEGER NOT NULL DEFAULT 0,
    expires_on  TEXT NOT NULL DEFAULT '',            -- '' = never; else YYYY-MM-DD (inclusive)
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_discount_codes_tenant_code
    ON discount_codes(tenant_id, code);

-- What was applied to an invoice (kept for display/receipts). amount_cents/tax_cents are
-- already the post-discount figures, so these are informational only.
ALTER TABLE invoices ADD COLUMN discount_code  TEXT    NOT NULL DEFAULT '';
ALTER TABLE invoices ADD COLUMN discount_cents  INTEGER NOT NULL DEFAULT 0;

-- 0051_gift_card_purchases — selling gift cards online (the revenue half of gift cards).
-- A visitor on the studio's public site buys a gift card for a recipient: this records a
-- PENDING purchase tied to a normal invoice; when that invoice is paid (any settle path),
-- mark_paid fulfills it — issuing a gift_cards row for the amount and emailing the code to
-- the recipient. Keeping the purchase as its own row (vs a flag on the invoice) means the
-- buyer/recipient details and the issued-card link live together, and fulfillment is a
-- single idempotent claim. Tenant-scoped, cascade-deleted.

CREATE TABLE IF NOT EXISTS gift_card_purchases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    invoice_id      INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount_cents    INTEGER NOT NULL DEFAULT 0,
    recipient_name  TEXT NOT NULL DEFAULT '',
    recipient_email TEXT NOT NULL DEFAULT '',
    buyer_name      TEXT NOT NULL DEFAULT '',
    buyer_email     TEXT NOT NULL DEFAULT '',
    message         TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',   -- 'pending' | 'fulfilled'
    gift_card_id    INTEGER REFERENCES gift_cards(id) ON DELETE SET NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gcp_invoice ON gift_card_purchases(invoice_id);

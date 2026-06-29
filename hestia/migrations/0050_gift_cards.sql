-- 0050_gift_cards — studio gift cards (stored value) redeemable at checkout.
-- A gift card is POST-TAX stored value applied to an invoice's grand total as a payment —
-- NOT a pre-tax discount. So redemption must never touch amount_cents/tax_cents (that would
-- corrupt revenue + sales tax); instead it draws the card's balance down and records the
-- credit in invoices.gift_credit_cents, and the amount charged becomes total − credit.
-- A ledger row per draw makes redemption auditable and reversible (on void/refund). Codes
-- are unique per studio; cards are single-currency. Tenant-scoped, cascade-deleted.

CREATE TABLE IF NOT EXISTS gift_cards (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    code          TEXT NOT NULL,                       -- normalized upper-case
    initial_cents INTEGER NOT NULL DEFAULT 0,          -- value at issue (never mutated)
    balance_cents INTEGER NOT NULL DEFAULT 0,          -- remaining; drawn down / restored
    currency      TEXT NOT NULL DEFAULT 'usd',
    active        INTEGER NOT NULL DEFAULT 1,
    expires_on    TEXT NOT NULL DEFAULT '',            -- '' = never; else YYYY-MM-DD inclusive
    note          TEXT NOT NULL DEFAULT '',            -- e.g. recipient (owner-facing)
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_gift_cards_tenant_code ON gift_cards(tenant_id, code);

-- One row per draw-down/reversal of a card against an invoice.
CREATE TABLE IF NOT EXISTS gift_card_redemptions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gift_card_id INTEGER NOT NULL REFERENCES gift_cards(id) ON DELETE CASCADE,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    amount_cents INTEGER NOT NULL,                     -- drawn down at redemption
    status       TEXT NOT NULL DEFAULT 'applied',      -- 'applied' | 'reversed'
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- one live redemption of a given card per invoice (idempotency / double-redeem guard)
CREATE UNIQUE INDEX IF NOT EXISTS idx_gcr_card_invoice ON gift_card_redemptions(gift_card_id, invoice_id);
CREATE INDEX IF NOT EXISTS idx_gcr_invoice ON gift_card_redemptions(invoice_id);

-- post-tax credit on the invoice = sum of live redemptions; amount_cents/tax_cents untouched
ALTER TABLE invoices ADD COLUMN gift_credit_cents INTEGER NOT NULL DEFAULT 0;

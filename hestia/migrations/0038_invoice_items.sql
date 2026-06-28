-- 0038_invoice_items — optional line items for an invoice. An invoice's amount_cents
-- stays the authoritative subtotal (so tax, totals, the pay link, payment plans, and A/R
-- are unchanged); when a studio enters line items, amount_cents is just computed as their
-- sum and the items ride along as a display breakdown shown on the invoice, pay page, and
-- receipt. A flat single-amount invoice simply has no rows here. Tenant-scoped,
-- cascade-deleted with the invoice (and the tenant).

CREATE TABLE IF NOT EXISTS invoice_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    description  TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL DEFAULT 0,
    position     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_invoice_items_invoice ON invoice_items(invoice_id, position);

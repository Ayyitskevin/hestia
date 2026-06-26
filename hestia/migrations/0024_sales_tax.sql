-- 0024_sales_tax — let a studio charge sales tax on invoices. The rate is stored
-- per-tenant in basis points (integer; 850 = 8.50%), and each invoice records the
-- tax it added in cents. amount_cents stays the pre-tax subtotal (that's what
-- revenue/P&L count), so tax is purely additive: the client pays amount + tax, and
-- tax collected is tracked separately as the liability it is. Default 0 → no tax,
-- so every existing invoice is unchanged.

ALTER TABLE tenants  ADD COLUMN tax_rate_bps INTEGER NOT NULL DEFAULT 0;
ALTER TABLE invoices ADD COLUMN tax_cents    INTEGER NOT NULL DEFAULT 0;

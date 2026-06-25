-- 0016_orders — purchasable offers that settle to a print-lab order.
--
-- A client reserving a bundle from the offer creates an `orders` row plus a
-- regular invoice (so it rides the existing idempotent /pay flow). When that
-- invoice is paid, the order flips to paid and a `fulfillment.submit` job sends
-- it to the print-fulfillment seam (mock by default), recorded in
-- `fulfillment_orders`. This is the last link in client-to-cash: the offer now
-- settles to a physical product instead of a disabled "reply to your photographer".

CREATE TABLE IF NOT EXISTS orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    offer_id     INTEGER REFERENCES offers(id) ON DELETE SET NULL,
    gallery_id   INTEGER REFERENCES galleries(id) ON DELETE SET NULL,
    invoice_id   INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    sku          TEXT NOT NULL,
    name         TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'usd',
    status       TEXT NOT NULL DEFAULT 'pending',   -- pending | paid
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fulfillment_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    order_id     INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    backend      TEXT NOT NULL DEFAULT 'mock',
    provider_ref TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'submitted',  -- pending | submitted | produced | shipped | failed
    detail       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_orders_gallery ON orders(gallery_id, created_at);
CREATE INDEX IF NOT EXISTS idx_orders_invoice ON orders(invoice_id);
-- One fulfillment per order: the durable pre-claim latch that stops an
-- at-least-once job retry from submitting a duplicate order to a real lab.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fulfillment_order ON fulfillment_orders(order_id);

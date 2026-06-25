-- 0014_sales_campaigns — time-limited sales on a gallery's offer.
--
-- The studio launches a sale (headline + a discount % + a deadline) on a
-- gallery's offer; while it's live the public offer page shows the urgency and
-- the discounted prices (applied live at render — the stored offer/token is
-- never mutated). One active campaign per gallery: launching a new one ends the
-- prior. This is the urgency-gated funnel half of the sales-campaign item,
-- built on the favorites/vision auto-curation already in the offer.

CREATE TABLE IF NOT EXISTS sales_campaigns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gallery_id   INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    headline     TEXT NOT NULL DEFAULT '',
    discount_pct INTEGER NOT NULL DEFAULT 0,     -- 0–90
    ends_at      TEXT NOT NULL,                  -- sale deadline
    status       TEXT NOT NULL DEFAULT 'active', -- active | ended
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sales_campaigns_gallery ON sales_campaigns(gallery_id, status);

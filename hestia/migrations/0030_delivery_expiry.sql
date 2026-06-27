-- 0030_delivery_expiry — let a studio put an expiry date on a gallery's download
-- link (common in the trade: "your gallery is available through <date>"), to bound
-- storage/bandwidth and nudge clients to grab their files. The link works through
-- the expiry date and 410s after. NULL = never expires, so every existing delivery
-- link keeps working unchanged.

ALTER TABLE galleries ADD COLUMN delivery_expires_at TEXT;   -- ISO date; null → never expires

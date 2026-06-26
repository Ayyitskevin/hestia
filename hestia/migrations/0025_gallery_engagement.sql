-- 0025_gallery_engagement — know whether a client actually opened and downloaded
-- their gallery. A view is counted when the delivery or proofing page is opened; a
-- download when the zip or an individual file is fetched. Plain counters plus a
-- last-seen timestamp, defaulted so existing galleries read as zero engagement until
-- activity arrives.

ALTER TABLE galleries ADD COLUMN view_count     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE galleries ADD COLUMN download_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE galleries ADD COLUMN last_viewed_at TEXT;

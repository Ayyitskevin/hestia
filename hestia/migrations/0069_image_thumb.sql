-- Downscaled browse thumbnail key for each image. NULL for images uploaded before
-- this migration (and for any upload where thumbnailing failed) — Storage.thumb_url()
-- and the media/delivery routes fall back to the full original when it's NULL, so old
-- galleries keep working, just without the bandwidth optimization on grids.
ALTER TABLE images ADD COLUMN thumb_key TEXT;

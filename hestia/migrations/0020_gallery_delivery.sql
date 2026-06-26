-- 0020_gallery_delivery — hand the client their finished high-res gallery.
-- One unguessable, rotatable download link per gallery (the same token model as
-- offers and portals), opt-in via a nullable token. No client login; the link is
-- the gate. Closes the "deliver the digital files" gap next to print sales.

ALTER TABLE galleries ADD COLUMN delivery_token TEXT;

CREATE INDEX IF NOT EXISTS idx_galleries_delivery ON galleries(delivery_token);

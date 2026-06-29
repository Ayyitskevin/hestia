-- 0052_image_hidden — make AI culling actionable.
-- Vision already flags near-duplicates and likely blinks (cull_summary); this lets the
-- studio APPLY those suggestions by hiding frames. A hidden image stays in the library
-- (and in analysis) but is excluded from the client gallery and from delivery — so culling
-- is reversible and never deletes the original. Defaults to 0 (visible), so existing images
-- are unaffected.

ALTER TABLE images ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;

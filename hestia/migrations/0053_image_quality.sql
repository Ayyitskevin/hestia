-- 0053_image_quality — deepen the vision pass with per-frame technical sub-scores.
-- The pass already rates a single keeper_score; these add the two signals photographers
-- cull on most: exposure (overall brightness — under/over) and sharpness (in focus vs soft/
-- motion-blurred). They drive owner-facing advisory flags (soft/dark/bright); existing cull
-- and keeper behaviour is unchanged. Nullable, so rows analysed before this stay valid (no
-- flag is shown for a NULL score).

ALTER TABLE image_analyses ADD COLUMN exposure REAL;
ALTER TABLE image_analyses ADD COLUMN sharpness REAL;

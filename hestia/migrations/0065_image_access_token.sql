-- 0065_image_access_token -- unguessable per-image capability token for the /media
-- serve route. Client image URLs used to be the storage key
-- (<tenant_id>/<gallery_id>/<image_id>.<ext>): the tenant id leaked in the <img src>
-- of any un-PINned published gallery, and gallery/image ids are sequential, so a
-- PIN-protected or delivery-expired gallery's originals could be enumerated. The
-- token decouples "can view THIS image" from the guessable key. Existing rows are
-- backfilled here; new rows set it in add_image via a CSPRNG token.

ALTER TABLE images ADD COLUMN access_token TEXT NOT NULL DEFAULT '';

-- Backfill: a distinct 128-bit token per existing row (randomblob is evaluated
-- per row). Only pre-launch demo/test images exist at this point.
UPDATE images SET access_token = lower(hex(randomblob(16))) WHERE access_token = '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_images_access_token ON images(access_token);

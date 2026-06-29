-- 0054_album_review — let the client review and approve the AI-arranged album.
-- An album is currently owner-only (generate + preview). These add the same opt-in,
-- unguessable-link model as gallery delivery/offers: review_token shares the album with
-- the client (read-only spreads), and approved_at records their one-way sign-off. Both
-- nullable, so existing albums are unaffected (no link, not yet approved).

ALTER TABLE albums ADD COLUMN review_token TEXT;
ALTER TABLE albums ADD COLUMN approved_at TEXT;

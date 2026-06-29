-- 0055_album_change_request — let the client ask for album changes, not just approve.
-- Album review was approve-only; this records the client's change note + when, so review is a
-- two-way conversation: the owner is notified, edits the album, and re-shares. Both nullable;
-- cleared on approval (the request is resolved). Existing albums are unaffected.

ALTER TABLE albums ADD COLUMN change_request TEXT;
ALTER TABLE albums ADD COLUMN change_requested_at TEXT;

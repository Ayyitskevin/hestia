-- Client "submit my selections" — closes the proofing → album/offer handoff.
-- NULL = the client has not yet finalized their proofing picks; a timestamp = they did.
-- Nullable, no default: additive and safe per doctrine (existing rows stay NULL).
ALTER TABLE galleries ADD COLUMN selections_submitted_at TEXT;

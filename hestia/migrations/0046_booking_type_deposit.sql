-- 0046_booking_type_deposit — an optional retainer per bookable session type.
-- A studio can require a deposit to secure a booking ("pay the retainer to hold your
-- date"). When set (> 0), requesting that session also raises a deposit invoice the
-- visitor pays through the existing /pay flow. Display-only stays the same; this is the
-- money side. Integer cents, defaulted to 0 (no deposit → unchanged behavior).

ALTER TABLE booking_types ADD COLUMN deposit_cents INTEGER NOT NULL DEFAULT 0;

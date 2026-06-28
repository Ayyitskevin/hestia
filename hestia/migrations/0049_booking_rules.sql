-- 0049_booking_rules — guardrails for self-serve booking availability.
-- Two per-studio knobs the slot generator honours so "leave booking on" is actually safe:
--   * minimum notice — don't offer a slot starting sooner than N hours from now (no
--     5-minutes-from-now bookings);
--   * buffer — keep N minutes clear on either side of an existing session (no back-to-back).
-- Both default to 0 (unchanged behaviour). Tenant-scoped (columns on tenants).

ALTER TABLE tenants ADD COLUMN booking_min_notice_hours INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tenants ADD COLUMN booking_buffer_minutes   INTEGER NOT NULL DEFAULT 0;

-- 0045_booking_types — a studio's publishable "book me" menu of session types.
-- Self-serve booking: a visitor on the public studio site picks one of these session
-- types and requests a time, which drops into the CRM as a lead + a proposed appointment
-- the owner confirms (reusing the existing confirm/reminder/calendar machinery). Reference
-- data the studio sets up once; soft-archived (active = 0) rather than deleted so the menu
-- can be tidied without losing history. price_cents is display-only for now (deposits come
-- in a later slice). Tenant-scoped, cascade-deleted with the tenant.

CREATE TABLE IF NOT EXISTS booking_types (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    title            TEXT NOT NULL DEFAULT '',
    description      TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL DEFAULT 'consultation',
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    price_cents      INTEGER NOT NULL DEFAULT 0,
    active           INTEGER NOT NULL DEFAULT 1,
    position         INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_booking_types_tenant
    ON booking_types(tenant_id, active, position);

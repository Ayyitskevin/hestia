-- 0047_availability_windows — the studio's weekly open hours for self-serve booking.
-- Each row is a recurring window on one weekday (e.g. Tue 09:00–17:00). The public
-- booking page turns these into real open slots for a chosen session type (stepped by the
-- session's length, excluding times that collide with existing sessions), and picking a
-- slot auto-confirms it. Times are minutes-since-midnight in the studio's local clock —
-- consistent with how appointment times are stored (naive local), so no timezone math.
-- Tenant-scoped, cascade-deleted with the tenant.

CREATE TABLE IF NOT EXISTS availability_windows (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    weekday      INTEGER NOT NULL DEFAULT 0,   -- 0 = Monday .. 6 = Sunday (Python weekday())
    start_minute INTEGER NOT NULL DEFAULT 0,   -- minutes since midnight, local
    end_minute   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_availability_windows_tenant
    ON availability_windows(tenant_id, weekday, start_minute);

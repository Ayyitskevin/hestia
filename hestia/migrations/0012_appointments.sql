-- 0012_appointments — scheduling with client self-booking.
--
-- The studio proposes one or more time options for a session (consultation,
-- shoot, call); the client picks one via a public link and it's confirmed. The
-- confirmation and a day-before reminder are sent as durable jobs (the reminder
-- via the queue's run_at), and a confirmed booking emits appointment.confirmed
-- for the workflow engine. Booking is idempotent: proposed→confirmed once, so a
-- double submit or a re-opened link never rebooks. Same token model as the rest.

CREATE TABLE IF NOT EXISTS appointments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id        INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    project_id       INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title            TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'consultation',  -- consultation|shoot|call|other
    location         TEXT NOT NULL DEFAULT '',
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    status           TEXT NOT NULL DEFAULT 'proposed',      -- proposed|confirmed|canceled
    token            TEXT NOT NULL UNIQUE,                  -- public booking link
    starts_at        TEXT NOT NULL DEFAULT '',              -- the confirmed time (empty until booked)
    notes            TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The proposed time options the client chooses from.
CREATE TABLE IF NOT EXISTS appointment_options (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    appointment_id INTEGER NOT NULL REFERENCES appointments(id) ON DELETE CASCADE,
    tenant_id      TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    sequence       INTEGER NOT NULL DEFAULT 0,
    starts_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_appointments_tenant ON appointments(tenant_id, starts_at);
CREATE INDEX IF NOT EXISTS idx_appointments_project ON appointments(project_id);
CREATE INDEX IF NOT EXISTS idx_appointment_options_appt ON appointment_options(appointment_id, sequence);

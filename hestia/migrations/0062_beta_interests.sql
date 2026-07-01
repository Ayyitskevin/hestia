-- 0062_beta_interests -- public beta/waitlist lead capture.

CREATE TABLE IF NOT EXISTS beta_interests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL DEFAULT '',
    studio_name  TEXT NOT NULL DEFAULT '',
    email        TEXT NOT NULL UNIQUE,
    shoot_type   TEXT NOT NULL DEFAULT 'other',
    source       TEXT NOT NULL DEFAULT '',
    landing_path TEXT NOT NULL DEFAULT '',
    note         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'new',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_beta_interests_updated
    ON beta_interests(updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_beta_interests_source
    ON beta_interests(source, updated_at DESC);

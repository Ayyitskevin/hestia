-- 0018_testimonials — social proof: capture a client's review via an unguessable
-- link, then feature the best ones on the public studio site. Closes the loop
-- (deliver → ask → display) that turns a happy client into the next booking.

CREATE TABLE IF NOT EXISTS testimonials (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id    INTEGER,                              -- nullable: who we asked (FK-soft)
    token        TEXT NOT NULL UNIQUE,                 -- the public submit link
    author_name  TEXT NOT NULL DEFAULT '',
    rating       INTEGER NOT NULL DEFAULT 5,           -- 1..5 stars
    body         TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'requested',    -- requested | submitted | featured | hidden
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    submitted_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_testimonials_tenant ON testimonials(tenant_id, status);

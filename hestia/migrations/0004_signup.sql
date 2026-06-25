-- 0004_signup — self-serve signup support. Existing (admin-onboarded) users are
-- trusted, so the new column defaults to verified=1; self-signup users are
-- inserted with verified=0 and flip to 1 by consuming a verification token.

ALTER TABLE users ADD COLUMN verified INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS email_verifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_verif_token ON email_verifications(token_hash);

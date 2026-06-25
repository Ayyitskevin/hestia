-- 0003_password_resets — single-use, expiring password-reset tokens. Only the
-- keyed hash of a token is stored (same treatment as tenant API keys), so a
-- database leak never yields a usable reset link.

CREATE TABLE IF NOT EXISTS password_resets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_resets_token ON password_resets(token_hash);

-- 0013_gallery_proofing — client favorites and comments on delivered galleries.
--
-- When a client views their gallery they can heart the frames they love and leave
-- notes. Favorites are the explicit signal that later auto-curates sellable
-- packages (the vision signal says what's good; favorites say what the client
-- wants). Favorites are per gallery — a wedding gallery is one couple curating one
-- album — so (gallery_id, image_id) is unique and a heart toggles idempotently.

CREATE TABLE IF NOT EXISTS image_favorites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gallery_id INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    image_id   INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (gallery_id, image_id)
);

CREATE TABLE IF NOT EXISTS image_comments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gallery_id  INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    image_id    INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    author_name TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_image_favorites_gallery ON image_favorites(gallery_id);
CREATE INDEX IF NOT EXISTS idx_image_comments_gallery ON image_comments(gallery_id, image_id);

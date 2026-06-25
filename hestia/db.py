"""SQLite control-plane schema and connection helpers.

One boring SQLite file under ``HESTIA_DATA_DIR``, WAL mode, shared by request
handlers and the background pipeline worker.

Schema (one app, modules not microservices)
--------------------------------------------
- ``tenants``         — one studio: slug, name, shoot_type, plan
- ``users``           — email/password auth, role, tenant_id
- ``sessions``        — UI session cookies
- ``tenant_api_keys`` — hashed ``hestia_tk_<slug>_<secret>`` bearer keys
- ``galleries`` / ``images`` — native multi-tenant gallery hosting
- ``image_analyses`` — per-image vision output (keywords, keeper/hero, shot type)
- ``pipeline_runs``  — gallery → vision → offer run state (idempotent)
- ``offers``         — generated print/album offer + public share token
- ``audit_log``      — admin / pipeline actions
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    shoot_type  TEXT NOT NULL DEFAULT 'other',
    plan        TEXT NOT NULL DEFAULT 'beta',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'owner',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, email)
);

CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    token      TEXT NOT NULL UNIQUE,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    tenant_id  TEXT REFERENCES tenants(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'owner',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_api_keys (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    prefix     TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clients (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    email      TEXT NOT NULL DEFAULT '',
    phone      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id   INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    name        TEXT NOT NULL,
    shoot_type  TEXT NOT NULL DEFAULT 'other',
    status      TEXT NOT NULL DEFAULT 'lead',   -- lead|booked|shooting|delivered|archived
    event_date  TEXT,
    notes       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS galleries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id    INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    slug          TEXT NOT NULL,
    title         TEXT NOT NULL,
    client_name   TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'draft',     -- draft | published
    pin           TEXT,
    cover_image_id INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    published_at  TEXT,
    UNIQUE (tenant_id, slug)
);

CREATE TABLE IF NOT EXISTS images (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    gallery_id   INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL,
    storage_key  TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    width        INTEGER,
    height       INTEGER,
    bytes        INTEGER,
    position     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS image_analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    image_id      INTEGER NOT NULL UNIQUE REFERENCES images(id) ON DELETE CASCADE,
    gallery_id    INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    keywords_json TEXT NOT NULL DEFAULT '[]',
    keeper_score  REAL,
    hero_potential REAL,
    shot_type     TEXT,
    alt_text      TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source       TEXT NOT NULL DEFAULT 'gallery',
    source_id    TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'queued',    -- queued|running|done|error
    steps_json   TEXT NOT NULL DEFAULT '[]',
    offer_url    TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS offers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    gallery_id      INTEGER NOT NULL REFERENCES galleries(id) ON DELETE CASCADE,
    run_id          INTEGER REFERENCES pipeline_runs(id) ON DELETE SET NULL,
    token           TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL DEFAULT '',
    bundles_json    TEXT NOT NULL DEFAULT '[]',
    hero_images_json TEXT NOT NULL DEFAULT '[]',
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, gallery_id)
);

CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id    INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    project_id   INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title        TEXT NOT NULL,
    amount_cents INTEGER NOT NULL DEFAULT 0,
    currency     TEXT NOT NULL DEFAULT 'usd',
    status       TEXT NOT NULL DEFAULT 'draft',   -- draft|sent|paid|void
    token        TEXT NOT NULL UNIQUE,            -- public pay link
    provider     TEXT NOT NULL DEFAULT '',
    provider_ref TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    paid_at      TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_runs_tenant ON pipeline_runs(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_galleries_tenant ON galleries(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_images_gallery ON images(gallery_id, position);
CREATE INDEX IF NOT EXISTS idx_offers_token ON offers(token);
CREATE INDEX IF NOT EXISTS idx_clients_tenant ON clients(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_projects_tenant ON projects(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_projects_client ON projects(client_id);
CREATE INDEX IF NOT EXISTS idx_invoices_tenant ON invoices(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_token ON invoices(token);
"""

# Idempotent column additions for databases created before a column existed.
_MIGRATIONS = [
    ("galleries", "project_id", "ALTER TABLE galleries ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str | Path) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA_SQL)
        _apply_migrations(conn)
        conn.commit()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Add columns that post-date a table's original CREATE (idempotent)."""
    for table, column, ddl in _MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(ddl)


@contextmanager
def get_db(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def audit(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    tenant_id: str | None = None,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (tenant_id, actor, action, detail) VALUES (?, ?, ?, ?)",
        (tenant_id, actor, action, detail),
    )

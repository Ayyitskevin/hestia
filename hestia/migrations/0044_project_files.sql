-- 0044_project_files — the studio's per-project document workspace. The owner attaches
-- reference files to a project (a signed PDF, a shot list, a mood board, a vendor COI);
-- blobs live in storage, rows here. Owner-only for now (no public surface). Tenant-scoped,
-- cascade-deleted with the project (and the tenant).

CREATE TABLE IF NOT EXISTS project_files (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id    TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename     TEXT NOT NULL DEFAULT '',
    storage_key  TEXT NOT NULL DEFAULT '',
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    bytes        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id);

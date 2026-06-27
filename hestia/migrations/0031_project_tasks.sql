-- 0031_project_tasks — a per-project checklist so a studio never drops a deliverable
-- (send the contract, collect the deposit, deliver the gallery, order the album…).
-- Tasks belong to a project, toggle done/undone, and roll up to a progress count on
-- the project page. Tenant-scoped, cascade-deleted with their project.

CREATE TABLE IF NOT EXISTS project_tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    label      TEXT NOT NULL,
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_project_tasks_project ON project_tasks(tenant_id, project_id);

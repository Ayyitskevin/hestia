-- 0042_task_templates — reusable project-checklist templates per shoot type.
-- A studio sets up its go-to deliverable checklist once (per shoot type, or 'any' for all),
-- and Hestia copies the matching items onto a project's checklist when it books (and on
-- demand). Copying — not linking — means a template edit never rewrites past projects, and
-- re-applying is idempotent (items already on the project by label are skipped). The copied
-- rows are ordinary project_tasks. Tenant-scoped, cascade-deleted with the tenant.

CREATE TABLE IF NOT EXISTS task_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    shoot_type TEXT NOT NULL DEFAULT 'any',   -- a shoot type, or 'any' (applies to every project)
    label      TEXT NOT NULL DEFAULT '',
    position   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_task_templates_tenant
    ON task_templates(tenant_id, shoot_type, position);

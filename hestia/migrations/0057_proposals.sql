CREATE TABLE IF NOT EXISTS proposals (
    id             INTEGER PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    client_id      INTEGER,
    project_id     INTEGER,
    package_id     INTEGER,
    contract_id    INTEGER,
    invoice_id     INTEGER,
    title          TEXT NOT NULL,
    summary        TEXT NOT NULL DEFAULT '',
    terms          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'draft',
    token          TEXT NOT NULL UNIQUE,
    accepted_name  TEXT NOT NULL DEFAULT '',
    accepted_email TEXT NOT NULL DEFAULT '',
    accepted_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_proposals_tenant_status
    ON proposals (tenant_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_proposals_tenant_client
    ON proposals (tenant_id, client_id);

CREATE INDEX IF NOT EXISTS idx_proposals_tenant_project
    ON proposals (tenant_id, project_id);

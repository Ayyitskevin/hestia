-- 0007_contracts — studio contracts with a typed-signature e-sign flow.
--
-- A contract hangs off the client/project spine. The owner drafts terms, sends
-- it (which emails a public sign link), and the client signs by typing their
-- name and agreeing. Signing is idempotent: a contract transitions sent→signed
-- exactly once, capturing the typed signature, timestamp, and IP for the record.
-- A signed contract can never be re-signed or voided.

CREATE TABLE IF NOT EXISTS contracts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id      TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id      INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    project_id     INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title          TEXT NOT NULL,
    body           TEXT NOT NULL DEFAULT '',          -- the contract terms
    status         TEXT NOT NULL DEFAULT 'draft',     -- draft|sent|signed|void
    token          TEXT NOT NULL UNIQUE,              -- public sign link
    signer_name    TEXT NOT NULL DEFAULT '',          -- who is expected to sign
    signer_email   TEXT NOT NULL DEFAULT '',
    signature_name TEXT NOT NULL DEFAULT '',          -- the typed signature, captured at sign time
    signed_ip      TEXT NOT NULL DEFAULT '',
    signed_at      TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contracts_tenant ON contracts(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_contracts_project ON contracts(project_id);

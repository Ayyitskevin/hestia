-- 0033_contract_templates — a studio's reusable contract boilerplate. Instead of
-- retyping the same terms for every booking, a studio saves named contract templates
-- (a name + the body text) and starts a new contract from one. Tenant-scoped,
-- cascade-deleted with the tenant. The drafted contract is an independent copy — the
-- template is just a starting point, so editing one never changes the other.

CREATE TABLE IF NOT EXISTS contract_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contract_templates_tenant ON contract_templates(tenant_id);

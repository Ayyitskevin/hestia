-- 0040_service_packages — a studio's reusable "service menu" (offerings priced once).
-- Photographers re-type the same packages on every quote; a package is reference data they
-- set up once and pull into the invoice builder as a starting point. The invoice copies the
-- amount, so a package can be re-priced or archived later without touching past invoices.
-- Money is integer cents. Soft-archived (active = 0) rather than deleted, so tidying the
-- catalog never loses history. Tenant-scoped, cascade-deleted with the tenant.

CREATE TABLE IF NOT EXISTS service_packages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id     TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name          TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    price_cents   INTEGER NOT NULL DEFAULT 0,
    deposit_cents INTEGER NOT NULL DEFAULT 0,
    active        INTEGER NOT NULL DEFAULT 1,
    position      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_service_packages_tenant
    ON service_packages(tenant_id, active, position);

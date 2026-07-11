-- 0066_offer_catalog — per-studio print/album offer pricing.
--
-- Studios set their own bundle prices and per-favorite print rate. Stored as JSON
-- overrides merged with Hestia's defaults at offer-build time; empty JSON means
-- "use defaults". Tenant-scoped on the shared spine.

ALTER TABLE tenants ADD COLUMN offer_catalog_json TEXT NOT NULL DEFAULT '';
ALTER TABLE tenants ADD COLUMN favorite_print_cents INTEGER NOT NULL DEFAULT 1500;

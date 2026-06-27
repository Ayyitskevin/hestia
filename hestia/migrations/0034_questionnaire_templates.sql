-- 0034_questionnaire_templates — a studio's reusable intake question sets. A studio
-- asks the same questions every booking (wedding intake, newborn prep, senior-session
-- prep), so instead of retyping them it saves a named template (a name + the questions,
-- one per line) and starts a new questionnaire from one. Tenant-scoped, cascade-deleted
-- with the tenant. The drafted questionnaire is an independent copy — the template is
-- just a starting point, so editing one never changes the other.

CREATE TABLE IF NOT EXISTS questionnaire_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    prompts    TEXT NOT NULL DEFAULT '',   -- one question per line (same format as the create form)
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_questionnaire_templates_tenant ON questionnaire_templates(tenant_id);

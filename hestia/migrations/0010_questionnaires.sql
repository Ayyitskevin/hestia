-- 0010_questionnaires — client intake forms (event details, vision, timeline).
--
-- The studio drafts a questionnaire (a title + a list of prompts), sends it
-- (which emails a public fill link), and the client answers online. Submitting
-- is idempotent: the questionnaire transitions sent→completed exactly once,
-- capturing the answers; a re-opened link then shows the submitted answers
-- read-only. Same token + single-transition model as contracts and signing.

CREATE TABLE IF NOT EXISTS questionnaires (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id  TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    client_id  INTEGER REFERENCES clients(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    title      TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'draft',   -- draft | sent | completed | void
    token      TEXT NOT NULL UNIQUE,            -- public fill link
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Each prompt is one row; its answer is filled in place when the client submits.
CREATE TABLE IF NOT EXISTS questionnaire_items (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    questionnaire_id INTEGER NOT NULL REFERENCES questionnaires(id) ON DELETE CASCADE,
    tenant_id        TEXT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    sequence         INTEGER NOT NULL DEFAULT 0,
    prompt           TEXT NOT NULL,
    answer           TEXT NOT NULL DEFAULT '',
    answered_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_questionnaires_tenant ON questionnaires(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_questionnaires_project ON questionnaires(project_id);
CREATE INDEX IF NOT EXISTS idx_questionnaire_items_q ON questionnaire_items(questionnaire_id, sequence);

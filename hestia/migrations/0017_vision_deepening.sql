-- 0017_vision_deepening — deeper culling signals + per-studio AI style profile.
--
-- Each frame now carries an eyes-closed/blink likelihood and a content duplicate
-- key, so the culler can flag blinks and keep only the best of a duplicated frame.
-- A studio (Studio Pro tier) can set a free-text vision_style that biases the
-- keeper/hero scoring — woven into the prompt for the xai backend, and a
-- deterministic re-weight for the mock. All mock-first: the signals are produced
-- deterministically today and light up for real when HESTIA_XAI_API_KEY is set.

ALTER TABLE image_analyses ADD COLUMN eyes_closed REAL NOT NULL DEFAULT 0;
ALTER TABLE image_analyses ADD COLUMN dup_key TEXT NOT NULL DEFAULT '';

ALTER TABLE tenants ADD COLUMN vision_style TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_image_analyses_dup ON image_analyses(gallery_id, dup_key);

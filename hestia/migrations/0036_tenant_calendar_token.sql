-- 0036_tenant_calendar_token — an unguessable token per studio for a subscribe-able
-- calendar feed. A calendar app (Google/Apple/Outlook) fetches the .ics URL with no
-- session cookie, so the session-gated /schedule/calendar.ics can't be subscribed to;
-- this token authorizes the public /calendar/{token}.ics feed instead. NULL until the
-- owner first opens the subscribe link (lazily minted); regenerating it revokes the old
-- URL. Unique so a token maps to exactly one studio.

ALTER TABLE tenants ADD COLUMN calendar_token TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_calendar_token
    ON tenants(calendar_token) WHERE calendar_token IS NOT NULL;

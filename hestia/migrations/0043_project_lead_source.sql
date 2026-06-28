-- 0043_project_lead_source — capture "how did you hear about us?" on a lead, so the
-- studio can see which marketing channels actually drive inquiries and bookings. Set from
-- the public inquiry form; blank for manually-created projects (reported as "Unknown").
-- Additive, nullable-by-default. Tenant data lives on the existing projects row.

ALTER TABLE projects ADD COLUMN lead_source TEXT NOT NULL DEFAULT '';

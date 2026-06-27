-- 0027_email_signature — let a studio sign its outgoing client emails. The
-- signature is per-tenant free text (name, studio, phone, site) appended to the
-- body of client-facing notifications (confirmations, reminders, invoices,
-- galleries, questionnaires…) so every message sounds like the studio, not a
-- robot. Default '' → no signature appended, so every existing email is unchanged
-- and platform/owner mail (signup verify, password reset, lead alerts) stays plain.

ALTER TABLE tenants ADD COLUMN email_signature TEXT NOT NULL DEFAULT '';

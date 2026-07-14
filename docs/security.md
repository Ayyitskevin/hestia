# Hestia security playbook

How Hestia protects a photographer's clients, media, and money — the invariants, how
they're enforced, how to verify them, and what to do if something goes wrong. This is
the operator + contributor reference; it's kept honest (every claim maps to code and a
test).

## Threat model in one paragraph

Hestia is a multi-tenant hosted SaaS. The assets worth protecting are, in order:
**client media and PII** (private galleries, contracts, questionnaires), **money**
(client invoice payments and studio subscriptions), and **tenant isolation** (no studio
ever sees another's data). The main adversaries are a curious/malicious visitor, one
tenant probing another, and anyone who gets hold of a leaked link or a database dump.

## Invariants (and where they're enforced)

**Tenant isolation.** Every query is tenant-scoped; the tenant is derived server-side
from the session, never from a request parameter. Foreign-key joins are tenant-matched
so a stray cross-tenant id is dropped, not surfaced. Storage blobs are tenant-prefixed
(`<tenant>/…`) with a path-traversal guard. *Tests: `test_tenant_isolation.py`.*

**Client links are unguessable capability tokens.** Delivery, portal, pay, sign,
album-review, questionnaire, offer, and per-image `/media` links are gated by 256-bit
tokens — the token is the credential. Auth-link tokens (verify/reset/invite) are stored
**hashed** with a pepper, single-use, and expiring. Minting is idempotent and race-safe;
rotation instantly revokes the prior link. *Tests: `test_seo_privacy.py`,
`test_resets.py`, `test_interest.py`.*

**Private surfaces never get indexed.** `robots.txt` disallows every token prefix and
each token page carries `noindex`; sensitive and authenticated responses also carry
`X-Robots-Tag: noindex, nofollow, noarchive` and `Cache-Control: no-store`. CI enforces
the crawler rules and probes the live domain. Access logs redact the credential tail
of token paths. *Tests: `test_csp.py`, `test_seo_privacy.py`;
enforced by `scripts/ci-smoke.sh`.*

**Media can't execute or enumerate.** Uploaded images are served with a raster-type
allowlist (a stored `text/html`/SVG downloads as octet-stream, never runs). Public
image URLs are per-image capability tokens; the enumerable storage-key path is
owner-only. S3 media must remain private and is served with short-lived presigned URLs;
boot and hosted preflight reject the legacy public/CDN base URL configuration. Culled
or hidden frames never resurface to a client. Uploads are size-bounded (75 MB/image,
bounded read) so one studio can't OOM the box. *Tests: `test_storage_s3.py`,
`test_upload_hardening.py`, `test_tenant_isolation.py`.*

**Passwords & sessions.** Passwords are PBKDF2-HMAC-SHA256 at the OWASP-current work
factor, re-hashed up on login if stored weaker. Sessions are fresh per login (no
fixation), cookies are `httponly` + `samesite=lax` + `secure` (prod). Admin auth is a
constant-time master-token compare; owner sessions can't reach admin. Every user session
must still match an existing user, tenant, and role; malformed or stale session rows are
revoked. *Tests: `test_auth_context.py`, `test_auth_kdf.py`.*

**Money is server-authoritative and idempotent.** Order/invoice amounts are recomputed
server-side (the client's posted price is never trusted). The Stripe webhook verifies
the HMAC signature + replay window before acting, is idempotent (a redelivered event
never double-settles), acknowledges unknown-tenant events with 200 (no retry storm), and
only a **payment-mode** checkout settles an invoice. *Tests: `test_webhooks.py`,
`test_subscriptions.py`, `test_payments.py`.*

**Input is bounded & injection-safe.** Public free-text is length-capped at the data
layer; CSV exports neutralize spreadsheet-formula injection; calendar output is
RFC-5545 escaped; every template output is Jinja-autoescaped. Per-IP rate limits cover
login, signup, reset, inquiry, booking, and checkout. The limiter keys on the real
client IP: `X-Forwarded-For` is only trusted `HESTIA_TRUSTED_PROXIES` hops deep (1 for
the Caddy deploy, read from the right), so a spoofed header can't dodge the limits;
without a trusted proxy the header is ignored entirely. Limiter identity state is capped
and fails closed under a high-cardinality flood. *Tests: `test_ratelimit.py`.*

**Defense-in-depth headers.** Every response carries `nosniff`, `X-Frame-Options`,
`Referrer-Policy`, and a **Content-Security-Policy** with a per-request nonce —
`script-src` is nonce-only (no `'unsafe-inline'`), plus `object-src 'none'`,
`frame-ancestors 'none'`, `base-uri`/`form-action 'self'`. *Tests: `test_csp.py`.*

**Everything sensitive is audited.** Money, media, access, and credential changes
(including password reset) write an `audit_log` row attributable to an actor.

## Secrets & `.env`

- Generate each secret with `openssl rand -hex 32`: `HESTIA_API_TOKEN`,
  `HESTIA_TENANT_KEY_PEPPER`, `HESTIA_SESSION_SECRET`. Never commit real values;
  `.env` is git-ignored — `chmod 600 .env`.
- Live Stripe + SMTP credentials live only in `.env` on the box. See
  `docs/deploy-wiring.md`. Preflight **fails** on default/placeholder secrets, and in
  SaaS mode the app itself **refuses to start** with any CHANGE_ME secret — a
  misconfigured box fails closed at startup rather than serving forgeable CSRF tokens.
  *Tests: `test_fail_closed.py`.*
- Secrets appear only in outbound auth headers and crypto — never in a log line,
  template, error, or JSON response. `config_warnings` names a bad secret, never prints
  its value (`test_config.py`).
- Rotating `HESTIA_SESSION_SECRET` invalidates all sessions; rotating
  `HESTIA_TENANT_KEY_PEPPER` invalidates tenant API keys + outstanding invite/reset
  tokens. Rotate on suspected compromise.

## Dependency & supply-chain hygiene

CI runs `pip-audit` on every push (advisory — it surfaces known-CVE dependencies in the
log without blocking on an unfixable transitive/tooling CVE). Review it and raise the
offending constraint in `pyproject.toml` (as we did for `cryptography>=43.0.1`). Also run
it before each release and monthly (see `docs/operations.md`):

```sh
pip-audit --skip-editable                   # known-CVE check; installed by requirements/dev.lock
docker compose build --pull                 # pick up base-image (python:3.12-slim) patches
```

To make a specific finding blocking once triaged, drop `continue-on-error` from the CI
step; ignore an unfixable one with `pip-audit --ignore-vuln <ID>`.

## Verifying the whole posture

```sh
bash scripts/ci-smoke.sh                     # ruff + full test suite + live-boot privacy probes
python -m pytest tests/test_tenant_isolation.py tests/test_upload_hardening.py \
  tests/test_auth_kdf.py tests/test_webhooks.py tests/test_csp.py tests/test_seo_privacy.py -q
bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"   # live gates
```

## Incident response

1. **Suspected data exposure** → identify the surface (token leak? misconfig?). Rotate
   the relevant secret (pepper for tokens/API keys, session secret for sessions).
   Regenerate affected delivery/portal links (rotation revokes the old one).
2. **Payment anomaly** → Stripe dashboard is the source of truth; the webhook is
   idempotent, so re-delivering events is safe. Check the `audit_log`.
3. **Account compromise** → reset the password (kills all that user's sessions) and
   review `audit_log` for that tenant.
4. **Take a backup first** for anything destructive (`docs/backup-restore.md`).

## Responsible disclosure

Found a vulnerability? Email **security@hestia** (or the founder contact in the repo)
with steps to reproduce. Please don't open a public issue or test against other
studios' live data. We'll acknowledge and work a fix; good-faith research is welcome.

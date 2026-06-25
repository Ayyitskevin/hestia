# Hestia — Phase 0 scope

**One-line promise:** upload a gallery and get a client-ready print & album offer
link in seconds — in one app, no fleet of services.

Phase 0 proves the magic moment as a single consolidated product. It is **not**
live billing, public signup, or cloud storage — those are Phase 1.

## Context

This repo started as an orchestration shell over six separate services. After
reading all six (see [`SUITE-RESEARCH.md`](SUITE-RESEARCH.md)) the plan changed to
**consolidation**: the services duplicate identity/billing/storage six times and the
gallery→offer loop already worked without a shell. Hestia is now one multi-tenant
app with the AI engines as in-process modules.

## IN (this phase)

- One FastAPI + Jinja2 + HTMX + SQLite app on port **8500**
- Multi-tenant studios: tenants, users, sessions, `hestia_tk_*` API keys
- **Native gallery hosting**: create gallery → upload images → object storage
  (local backend, S3/R2-ready interface) → PIN-gated client delivery
- **Vision module** (`hestia/vision.py`): pluggable provider — deterministic
  `mock` (no key) or live xAI Grok — culls keepers, picks heroes, keywords frames
- **Sales module** (`hestia/sales.py`): builds print/album bundles from the vision
  signal and shoot type; mints **one idempotent** shareable client offer per gallery
- **Pipeline** (`hestia/pipeline.py`): gallery → vision → offer, persisted and
  idempotent, with a live stepper UI
- Admin onboarding, dashboard, gallery UI, public client offer page
- Billing **scaffold** (`hestia/billing.py`) — plans only
- `/healthz`, `scripts/ci-smoke.sh`, `scripts/dogfood-hestia.sh`, GitHub Actions

## OUT (deferred — Phase 1+)

- Live Stripe checkout on offers (scaffold only now)
- Public self-service signup (`HESTIA_SIGNUP_ENABLED=false`)
- Cloud object storage (S3/R2) — local filesystem for now
- The album-design module (essence of Mnemosyne)
- Marketing-copy and e-commerce-packshot product lines (Dionysus/Aphrodite — out of scope)
- White-label domains, mobile apps

## Magic moment

Upload a gallery, click **Process**, and within seconds the stepper goes
vision → offer and renders a real, clickable client offer URL. Re-process: same
link, never a duplicate. That is the whole product in one screen.

## First PR checklist

- [x] `uvicorn hestia.main:app --port 8500` boots
- [x] Admin onboards a studio; owner logs in
- [x] Create gallery → upload frames → process → vision + offer
- [x] Public client offer page renders bundles
- [x] **Idempotent**: double-process yields exactly one offer (test-proven)
- [x] `scripts/dogfood-hestia.sh` drives the magic moment live (no fleet)
- [x] `scripts/ci-smoke.sh` green (ruff + pytest + healthz)

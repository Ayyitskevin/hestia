# Hestia architecture

## One app, modules not microservices

Hestia is a single multi-tenant FastAPI application. The AI capabilities that were
separate services in the original suite are **in-process modules** here. This
removes the suite's biggest problem — identity, billing, and storage reimplemented
six times — and turns the "magic moment" from a chain of HTTP calls into a function
call. Rationale and evidence: [`SUITE-RESEARCH.md`](SUITE-RESEARCH.md).

## Data flow

```text
Admin onboards a studio (or a studio self-signs up, gated + email-verified)
        ↓
Owner creates gallery → uploads images (→ object storage: local | S3/R2)
        ↓
Owner clicks Process  →  POST /galleries/{id}/process
        ↓
   enqueue a durable job  →  hestia.jobs (SQLite-backed queue)
        ↓
┌──────────────────────────────────────────────────────┐
│ worker drains the queue → hestia.pipeline.execute_run  │
│   1. vision  → hestia.vision.analyze_gallery           │
│               (mock | xAI Grok) → keepers, heroes, kw  │
│   2. offer   → hestia.sales.create_or_update_offer     │
│               (idempotent: one token per gallery)      │
└──────────────────────────────────────────────────────┘
        ↓
Live stepper (poll /api/pipeline/runs/{id}) → offer URL
        ↓
Client opens /s/{slug}/{token}  →  print & album bundles → pay → invoice paid
```

Processing is enqueued, not run inline: a request `BackgroundTask` kicks an
immediate drain, and a worker thread (started in the app lifespan) is the durable
backstop — it retries with backoff and reclaims jobs orphaned by a crash, so a
run survives a restart. See [`jobs.py`](../hestia/jobs.py).

## Pluggable seams (mock by default, real on config)

| Seam | Env | mock (default) | real |
|------|-----|----------------|------|
| Vision | `HESTIA_VISION_BACKEND` | deterministic scores | xAI Grok |
| Album / Content / Product | `HESTIA_*_BACKEND` | plan only | xAI |
| Storage | `HESTIA_STORAGE_BACKEND` | local filesystem | S3 / R2 / MinIO |
| Payments | `HESTIA_PAYMENTS_BACKEND` | simulated checkout | Stripe Checkout + webhook |
| Subscriptions | `HESTIA_SUBSCRIPTION_BACKEND` | activate plan instantly | Stripe subscription + webhook |
| Email | `HESTIA_EMAIL_BACKEND` | record to outbox, send nothing | SMTP |

Every seam keeps the whole flow testable in CI with no keys, and degrades safely
when a real backend errors.

## Modules

| Module | Responsibility |
|--------|----------------|
| `tenants.py` | studios, users, API keys |
| `auth.py` / `csrf.py` | session + bearer auth; CSRF tokens for form POSTs |
| `galleries.py` + `storage.py` | native gallery + image hosting (local/S3) |
| `vision.py` | cull / keyword / hero scoring (pluggable) |
| `sales.py` | print/album bundles + idempotent client offers |
| `albums.py` / `products.py` / `content.py` | album spreads, packshot variants, marketing copy |
| `pipeline.py` | run state machine (gallery → vision → offer) |
| `jobs.py` | durable SQLite job queue + worker (retries, reclaim) |
| `invoices.py` / `payments.py` | invoices + checkout (mock/Stripe) |
| `billing.py` / `subscriptions.py` | plans + studio subscriptions (mock/Stripe) |
| `email.py` | transactional email seam + outbox |
| `studio.py` | public studio site + inquiry intake |
| `resets.py` / `verifications.py` | password reset + signup email verification tokens |
| `ratelimit.py` | in-process sliding-window limiter on the public surface |
| `obs.py` | structured (JSON) logging + per-request ids |
| `features.py` | shoot-type → offer/album tuning |
| `db.py` | SQLite control plane + numbered-SQL migration runner |

## Control-plane schema

Schema lives in numbered `.sql` files under [`hestia/migrations/`](../hestia/migrations);
`init_db` applies any not yet recorded in the `schema_migrations` ledger, in order,
once each (see `db.py`).

| Table | Purpose |
|-------|---------|
| `tenants` | studio: slug, name, shoot_type, plan |
| `users`, `sessions` | owner auth (incl. `verified`), UI sessions |
| `tenant_api_keys` | hashed `hestia_tk_*` bearer keys |
| `clients`, `projects` | CRM |
| `galleries`, `images` | native gallery hosting (images → object storage) |
| `image_analyses` | per-image vision output |
| `pipeline_runs` | run state (unique per tenant+gallery) |
| `offers` | client offer: token, bundles (**unique per gallery**) |
| `albums`, `product_sets`, `content_packs` | module outputs |
| `invoices` | billing + public pay token |
| `subscriptions` | studio plan / status / provider |
| `studio_profiles` | public site content |
| `emails` | transactional email outbox |
| `password_resets`, `email_verifications` | single-use, hashed-at-rest tokens |
| `jobs` | durable job queue |
| `audit_log` | tenant lifecycle events (surfaced at /settings/activity) |
| `schema_migrations` | applied-migration ledger |

## Idempotency (a deliberate fix)

The real Plutus mints a fresh share link on every call — re-processing a gallery
duplicates the client offer. Hestia guarantees the opposite: `pipeline_runs` and
`offers` are both unique on `(tenant, gallery)`, with the public token created once
and **reused** on every re-run. Proven in
`tests/test_app.py::test_double_process_one_offer_over_http`.

## Security posture

- Session cookies (UI) + `hestia_tk_*` bearer (API) + master admin token.
- Passwords PBKDF2; API keys, reset/verify tokens stored as keyed hashes.
- CSRF tokens on every authenticated form POST; the bearer API is exempt.
- Sliding-window rate limits on login, signup, inquiry, checkout, password reset.
- Baseline security headers on every response; per-request ids in structured logs.

## Failure model

| Failure | Behavior |
|---------|----------|
| Vision / offer error | run → `error` at that step; no offer minted |
| Job handler raises | recorded on the job, retried with backoff, then `error` |
| Worker crash mid-job | the job is reclaimed and re-queued on next boot |
| Real backend (Stripe/SMTP/xAI) error | degrades to the safe path; never 500s the request |
| Re-process | reuses the completed vision + the single offer token |

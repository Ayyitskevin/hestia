# Hestia architecture

## One app, modules not microservices

Hestia is a single multi-tenant FastAPI application. The AI capabilities that were
separate services in the original suite are **in-process modules** here. This
removes the suite's biggest problem — identity, billing, and storage reimplemented
six times — and turns the "magic moment" from a chain of HTTP calls into a function
call. Rationale and evidence: [`SUITE-RESEARCH.md`](SUITE-RESEARCH.md).

## Data flow (Phase 0)

```text
Admin onboards studio (tenant + owner + shoot_type)
        ↓
Owner creates gallery → uploads images (→ object storage)
        ↓
Owner clicks Process  →  POST /galleries/{id}/process
        ↓
┌──────────────────────────────────────────────────────┐
│ hestia.pipeline.execute_run  (background, persisted)   │
│   1. vision  → hestia.vision.analyze_gallery           │
│               (mock | xAI Grok) → keepers, heroes, kw  │
│   2. offer   → hestia.sales.create_or_update_offer     │
│               (idempotent: one token per gallery)      │
└──────────────────────────────────────────────────────┘
        ↓
Live stepper (poll /pipeline/{id}/partial) → offer URL
        ↓
Client opens /s/{slug}/{token}  →  print & album bundles
```

## Modules

| Module | Responsibility | Essence of |
|--------|----------------|-----------|
| `tenants.py` | studios, users, API keys | (new control plane) |
| `galleries.py` + `storage.py` | native gallery + image hosting | Mise galleries |
| `vision.py` | cull / keyword / hero scoring (pluggable provider) | Argus |
| `sales.py` | print/album bundles + idempotent client offers | Plutus |
| `pipeline.py` | run state machine (gallery → vision → offer) | the dogfood loop |
| `billing.py` | plan scaffold (Phase 1 = live Stripe) | (new) |
| `features.py` | shoot-type → offer/album tuning | suite presets |

## Control-plane schema

| Table | Purpose |
|-------|---------|
| `tenants` | studio: slug, name, shoot_type, plan |
| `users`, `sessions` | owner auth, UI sessions |
| `tenant_api_keys` | hashed `hestia_tk_*` bearer keys |
| `galleries`, `images` | native gallery hosting (images → object storage) |
| `image_analyses` | per-image vision output (keywords, keeper/hero, shot type) |
| `pipeline_runs` | run state, steps JSON, offer URL (unique per tenant+gallery) |
| `offers` | client offer: token, bundles, hero images (**unique per gallery**) |
| `audit_log` | admin / pipeline actions |

## Idempotency (a deliberate fix)

The real Plutus mints a fresh share link on every call — re-processing a gallery
duplicates the client offer. Hestia guarantees the opposite: `pipeline_runs` is
unique on `(tenant, gallery)` and `offers` is unique on `(tenant, gallery)` with the
public token created once and **reused** on every re-run. Proven in
`tests/test_pipeline.py::test_double_run_yields_exactly_one_offer`.

## Storage seam

`storage.py` is a tiny interface (`put/open/exists/delete/public_path`) with a
local-filesystem backend today and an S3/R2 backend in Phase 1. Keys are always
tenant-scoped (`<tenant_id>/<gallery_id>/<image_id>.<ext>`).

## Vision provider seam

`vision.py` selects a provider by `HESTIA_VISION_BACKEND`: `mock` (deterministic,
no key — used by tests, CI, and demos) or `xai` (live Grok vision). Adding another
provider is one class with an `analyze(filename, data) -> VisionResult` method.

## Failure model

| Failure | Behavior |
|---------|----------|
| Vision provider error | run → `error` at the vision step; no offer minted |
| Offer build error | run → `error` at the offer step |
| Re-process | reuses the completed vision + the single offer token |

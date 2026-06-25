# Hestia architecture

## Role in the suite

Hestia is the **control plane and product shell**. It does not replace gallery storage,
vision models, print catalogs, or album layout engines — it **orchestrates** them.

## Modes

| Mode | Flag | Notes |
|------|------|-------|
| SaaS shell | `HESTIA_SAAS_MODE=true` | Default; port 8500 |

## Data flow (Phase 0)

```text
Admin creates tenant + shoot_type
        ↓
Operator triggers POST /api/pipeline/run { source, source_id }
        ↓
┌───────────────────────────────────────────────────┐
│ hestia.pipeline                                    │
│   1. vision   → Argus                             │
│   2. recommend → Plutus → offer_url               │
│   3. album    → Mnemosyne (if enabled)              │
│   4. campaign → Dionysus (if enabled)             │
└───────────────────────────────────────────────────┘
        ↓
Dashboard stepper + links for operator
```

## Control-plane schema (planned)

| Table | Purpose |
|-------|---------|
| `tenants` | Studio slug, name, shoot_type, plan |
| `users` | Email auth, role, tenant_id |
| `sessions` | UI sessions |
| `service_credentials` | Per-tenant service URLs and tokens |
| `pipeline_runs` | Idempotent run state, steps JSON, outputs |
| `audit_log` | Admin actions |

## Failure model

| Failure | Behavior |
|---------|----------|
| Argus down | Pipeline fails at `vision`; no offer |
| Plutus down | Pipeline fails at `recommend`; surface error |
| Mnemosyne down | Mark `album` skipped; offer still valid |
| Dionysus down | Mark `campaign` skipped; offer still valid |

## Phase 1+ (not built in Phase 0)

- Unified Stripe Customer per Hestia tenant
- Direct upload path without Mise (`source: upload_batch` → Plutus)
- Public signup + email verification
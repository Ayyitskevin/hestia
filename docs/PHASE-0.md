# Hestia — Phase 0 scope

**One-line promise:** one login, one pipeline — publish a gallery and get a client-ready
print offer link without opening five browser tabs.

Phase 0 is **not** public signup, unified Stripe billing, or multi-tenant Mise. It proves the
orchestrated magic moment before we wire money and scale.

## IN

- FastAPI shell on port **8500**
- SQLite: tenants, users, sessions, service credentials, pipeline runs, audit log
- Admin: create tenant, set `shoot_type`, wire service URLs + tokens
- Onboarding: studio name → shoot type → health check
- Dashboard: service health strip + recent pipeline runs
- Pipeline orchestrator: `mise_gallery` → Argus → Plutus offer URL
- Optional steps when shoot type enables: Mnemosyne, Dionysus
- Pipeline UI stepper with live status
- `hestia/clients/*.py` typed HTTP wrappers
- `scripts/dogfood-hestia.sh` — E2E on operator fleet
- `scripts/ci-smoke.sh` + GitHub Actions
- `/healthz` aggregating sibling services

## OUT

- Public self-service signup (`HESTIA_SIGNUP_ENABLED` stays false)
- Live unified Stripe subscription
- Multi-tenant Mise rewrite
- Native gallery hosting (use Mise or Plutus upload paths)
- Embedded replacement of Plutus/Argus/Mnemosyne admin UIs
- White-label domains, mobile apps
- Full Mise CRM (Phase 2)

## Magic moment

Open Hestia after triggering a pipeline on a real gallery and think:
*"I'd send that offer link to a client right now — and I didn't touch Argus or Plutus directly."*

## First PR checklist

- [ ] `uvicorn hestia.main:app --port 8500` runs
- [ ] Admin login + tenant create + shoot type select
- [ ] `POST /api/pipeline/run` happy path → Plutus offer URL
- [ ] Pipeline stepper UI
- [ ] `dogfood-hestia.sh` passes (real or documented mock)
- [ ] CI green
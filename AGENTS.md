# AGENTS.md — instructions for AI coding agents

You are working on **Hestia**, the AI-native studio for photographers (port 8500).
It is **one multi-tenant app** — modules, not microservices.

## Read first

1. [`README.md`](README.md) — product brief
2. [`docs/PHASE-0.md`](docs/PHASE-0.md) — what's IN and OUT now
3. [`docs/architecture.md`](docs/architecture.md) — modules + data flow
4. [`docs/SUITE-RESEARCH.md`](docs/SUITE-RESEARCH.md) — why we consolidated (evidence)

## Hard rules

- **One app, in-process modules.** No HTTP calls to sibling services. Vision and
  sales are Python modules (`hestia/vision.py`, `hestia/sales.py`), not clients.
- **Idempotent offers.** One offer/token per gallery, reused on re-run. Never mint
  a duplicate client link (this is the bug we exist to not have — see the real
  Plutus in `SUITE-RESEARCH.md`).
- **Pluggable seams.** Vision is a provider interface (`mock` | `xai`); storage is
  an interface (`local` | `s3`). Add a backend, don't fork the caller.
- **Tenant-scoped everything.** Galleries, images, runs, offers are keyed by
  `tenant_id`; never leak across studios.
- **Phase 0 only** unless asked: no live Stripe, no S3, no public signup, no album
  module yet. Scaffold and document instead.
- **Match conventions.** FastAPI + Jinja2 + HTMX + SQLite(WAL); warm hearth UI, not
  generic purple SaaS.

## Before marking work complete

- [ ] `bash scripts/ci-smoke.sh` passes (ruff + pytest + healthz)
- [ ] `bash scripts/dogfood-hestia.sh` drives the magic moment green
- [ ] New env vars documented in `README.md` and `.env.example`
- [ ] Idempotency preserved (re-process → one offer)

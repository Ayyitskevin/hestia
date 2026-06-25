# AGENTS.md — instructions for AI coding agents

You are working on **Hestia**, the independent AI-native studio OS for photographers
(port 8500). It is **one multi-tenant modular monolith** — modules, not microservices.

## Read first

1. **[`docs/HESTIA-DOCTRINE.md`](docs/HESTIA-DOCTRINE.md)** — the canonical product
   truth. It wins over every other doc, comment, or instinct.
2. [`README.md`](README.md) — the product brief
3. [`docs/architecture.md`](docs/architecture.md) — modules + data flow
4. [`docs/SUITE-RESEARCH.md`](docs/SUITE-RESEARCH.md) — historical repo analysis
   (design reference only — see its reframe note)

## Hard rules

- **Hestia is its own product.** The old repos (Mise, Argus, Plutus, Mnemosyne,
  Dionysus, Aphrodite, Athena, Midas) are **design DNA, not dependencies.** Never add
  a runtime dependency, an HTTP call, or a code import to a sibling repo. Rebuild only
  the essence that belongs in Hestia.
- **Do not build six products in one repo.** Build *one* product with modules around
  *one* revenue workflow and *one* data spine. Bad: "Argus-in-Hestia". Good: "a Vision
  module on the shared tenant/gallery spine."
- **Build vertical SaaS slices.** Each change is a thin slice through the spine:
  data model → module → routes → templates → tests → PR. Prefer small slices over
  mega-rewrites; keep existing tests passing and the dogfood flow green.
- **Tenant-scoped everything.** Every meaningful row is keyed by `tenant_id`; never
  leak across studios. Tenant isolation is a tested invariant.
- **Idempotent money links.** One offer/token per gallery, reused on re-run — never
  mint a duplicate client link. Settling an invoice is idempotent. Every public token
  has a clear uniqueness + lifecycle policy.
- **Provider seams are mock-first.** AI/payments/storage/email are interfaces with a
  deterministic `mock` default (no keys, runs in CI), a real backend behind env, and
  safe degradation on error. The model proposes; **code validates**. Add a backend;
  don't fork the caller.
- **Forward-only migrations.** Add a new numbered `hestia/migrations/NNNN_*.sql`;
  never edit an applied one. Use explicit SQL and clear data-access functions.
- **Stay boring under the hood.** FastAPI + Jinja2 + HTMX + SQLite(WAL), durable
  jobs, simple CSS, warm-hearth UI. No premature React, no microservices, no
  speculative infrastructure, no generic purple SaaS.
- **Commercial filter.** Every feature must help the photographer book, deliver,
  sell, reduce admin drag, or improve the client experience. If not, shelve it.

## Before marking work complete

- [ ] `bash scripts/ci-smoke.sh` passes (ruff + pytest + healthz)
- [ ] `bash scripts/dogfood-hestia.sh` drives the magic moment green
- [ ] Tenant isolation preserved (no cross-studio reads); idempotency preserved
- [ ] New env vars documented in `README.md` and `.env.example`, matching `Settings`
- [ ] Docs and tests updated alongside any behavioral change

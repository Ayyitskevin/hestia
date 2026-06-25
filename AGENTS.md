# AGENTS.md — instructions for AI coding agents

You are working on **Hestia**, the unified photography studio SaaS shell (port 8500).

## Read first

1. [`README.md`](README.md) — canonical project brief
2. [`docs/PHASE-0.md`](docs/PHASE-0.md) — what is IN and OUT right now
3. [`docs/SUITE.md`](docs/SUITE.md) — HTTP contracts to sibling repos

## Hard rules

- **Phase 0 only** unless the user explicitly asks for Phase 1+ work.
- **Orchestrate, don't monolith** — call Argus/Plutus/Mnemosyne/Dionysus/Mise via `hestia/clients/`.
- **Idempotent pipelines** — never duplicate Plutus offer links on retry.
- **Graceful degradation** — Mnemosyne/Dionysus optional; Argus→Plutus is the critical path.
- **Horizontal product** — shoot-type presets toggle modules; no F&B-only branding in core UI.
- **Match suite patterns** — FastAPI + Jinja + HTMX; plutus-style bearer tokens; phase IN/OUT docs.

## Sibling repos (read-only reference)

| Repo | Path hint |
|------|-----------|
| plutus | `~/ai-workspace/plutus` |
| argus | `~/ai-workspace/argus` |
| mise | `~/ai-workspace/mise-work` |
| mnemosyne | `~/ai-workspace/mnemosyne` |
| dionysus | `~/ai-workspace/dionysus` |

## Before marking work complete

- [ ] Changes align with `docs/PHASE-0.md` IN/OUT
- [ ] `scripts/ci-smoke.sh` passes (once implemented)
- [ ] New env vars documented in `README.md` and `.env.example`
- [ ] Integration assumptions updated in `docs/SUITE.md` if contracts change
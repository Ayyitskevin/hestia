# Hestia codebase review and improvement plan

- **Original review:** 2026-07-13
- **Historical baseline:** `a3f31b9c722554ddbdfcd26882c19eabcb95ad75` (`main`)
- **Status refreshed:** 2026-07-17
- **Status base:** current branch at this document revision (includes `25029d1` and
  the 2026-07-17 live-vision resilience slice)
- **Scope:** preserve every product capability; improve correctness, security,
  reproducibility, maintainability, and launch confidence without changing Hestia's
  modular-monolith doctrine.

## Executive assessment (historical)

This assessment describes the frozen 2026-07-13 baseline above. Its counts and observed
failures remain as review evidence; they are not claims about current `main`. See the
status matrix below for the disposition of each finding.

Hestia is not a prototype that needs a rewrite. It is a coherent, unusually well-tested
vertical SaaS: roughly 25k application lines, 22k test lines, 69 forward migrations, and
1,289 passing tests. The right move is to harden the seams where duplicated policy has
drifted, deepen tests around the revenue spine, make builds reproducible, and close the
remaining gap between application-level readiness and the actual edge/deploy artifacts.

The architecture should remain:

- one FastAPI + Jinja2 + HTMX modular monolith;
- explicit SQLite/WAL state and durable jobs;
- tenant-scoped data access and storage namespaces;
- mock-first provider seams with real backends behind configuration;
- forward-only migrations;
- thin vertical slices rather than a framework or microservice rewrite.

No existing feature needs to be removed to do this work.

## Current status - 2026-07-17

| Review area | Status | Evidence and remaining gate |
|---|---|---|
| Private-surface policy | **Landed** | Canonical policy and regressions merged in PR #207. Media-token authorization semantics are a separate security/product decision and remain open. |
| Migration integrity | **Open - human gate** | The original-0065/checksum and partial-DDL design still requires schema approval, compatibility fixtures, and a live-data backup plan. |
| Revenue-spine coverage | **Landed; semantics open** | PR #217 made the selected-suite gate block the `smoke` job and pass: 192 tests, 83.23% coverage. No branch rule requires that job yet; Stripe settlement validation and other money-state invariants remain human-gated. |
| Custom-domain edge | **Open - human gate** | The public Caddy catch-all and on-demand TLS behavior still require infrastructure review and deployment approval. |
| Build reproducibility and wheel contents | **Landed** | PRs #209 and #213 added locked installs, artifact checks, and the missing runtime asset. |
| Runtime vulnerability enforcement | **Landed** | PR #218 made the exact runtime-lock audit block the `smoke` job. The broader development-lock audit remains advisory. |
| Deprecation enforcement | **Landed** | The `httpx` transition landed in PR #213; PR #219 removed the remaining Pillow warning and made every `DeprecationWarning` fail pytest. |
| Shared xAI transport | **Landed; paid canary open** | PR #211 consolidated the repeated transport/error seam. Commit `25029d1` added bounded streaming for image responses, the current JSON/data-URI edit contract, and a configurable current image model. A paid live canary still needs explicit approval and a bounded test image. |
| Product-render validation | **Landed** | Commit `25029d1` validates JPEG/PNG sources and provider output, exercises normal provider-sized rasters against every real preset, requires retained alpha for transparent output, and canonically crops/resizes/re-encodes before storage. |
| Pillow runtime and compatibility | **Landed** | Pillow 12.3 is a core runtime dependency, its exact floor remains hash-locked, and hosted CI checks the focused perceptual-duplicate and media-delivery paths at that floor. |
| Live-vision resilience | **Landed** | Vision chat responses and result fields are bounded before persistence. Typed xAI failures roll back all partial live rows, recompute the whole gallery with the deterministic mock, label the fallback, preserve offer creation, and retry live under the same offer token on reprocess. |
| Vision calibration snapshot | **Landed; benchmark open** | Every authenticated studio gallery view exports one spreadsheet-safe row per frame with model scores, derived decisions, current weak labels, and blank reviewer columns. It includes no images or capability URLs. Analyses are latest-state only and exact model/prompt/style-at-run provenance is not yet persisted, so a labeled paid/live quality benchmark remains human-gated. |
| Storage footprint visibility | **Landed; pricing open** | Owner Account and master-admin System views expose tenant-matched, overflow-safe original-image and project-file byte metadata with anomaly counts and explicit derived-object/provider-cost exclusions. It is a planning denominator only: dollars, quotas, and billing remain human-gated. |
| Restore and artifact evidence | **Partial** | PRs #212 and #213 added restore/artifact evidence. Offsite-sync freshness, media-backend integration, and Caddy adaptation evidence remain open. |
| Release and license truth | **Open - human gate** | License choice, tag history, and release metadata still require a legal/product decision. |

### Current priority map

- **High - human-gated:** decide the public pricing/BYOK story (the configurable hosted
  subsidy defaults to one live gallery up to 150 images; a studio key takes precedence,
  and deployments may disable the subsidy); decide media capability scope; validate
  Stripe settlement semantics; design the original-0065/checksum migration path; define
  offsite-sync freshness evidence; and review the public Caddy custom-domain edge.
- **Medium - autonomous GREEN:** make gallery publication idempotency and reminder
  rescheduling behavior explicit with regressions; keep provider result validation
  domain-local; update competitive claims whenever shipped depth changes.
- **Medium - human-gated after design:** subscription terminal-state ordering,
  fulfillment retry/payment semantics, gallery-PIN authorization/rate limiting,
  timezone/calendar schema, repository rulesets, and release/license metadata.
- **Completed since the previous refresh:** Pillow floor coverage, strict content result
  validation, product-render validation, xAI image-contract correction, live-vision
  whole-gallery resilience, the studio calibration snapshot, tracked storage
  visibility, and availability slot deduplication.

## Historical verified baseline (frozen 2026-07-13)

The following evidence is intentionally preserved as observed at commit `a3f31b9`. Later
rows and prose in this section are historical even where the current status is now green.

| Gate | Result | Evidence |
|---|---|---|
| Clean checkout | Pass | `main...origin/main`, no tracked or untracked changes after audit cleanup |
| Lint | Pass | `ruff check hestia tests` via `scripts/ci-smoke.sh` |
| Full tests | Pass | **1,289 passed** in 204.56s on Python 3.12.13 |
| Boot/readiness smoke | Pass | `/healthz` reported DB `ok`; privacy probes passed |
| Magic moment | Pass | gallery upload -> vision -> offer in 0.4s; repeat reused the same URL |
| Dependency audit | Pass | `pip-audit --skip-editable`: no known vulnerabilities |
| Revenue-spine coverage | **Fail** | 69.04% vs the repository's 70% gate; 71 selected tests passed |
| Wheel build | Partial | wheel builds, but omits `hestia/static/og-cover.png` |
| GitHub backlog | Clear | no open issues or pull requests; one stale remote security branch remains |

The full suite emits one forward-compatibility warning: Starlette's TestClient path is
deprecating its `httpx` backend in favor of `httpx2`.

## Historical findings, ordered by original risk

The headings and bodies below are frozen 2026-07-13 findings, not the current risk order.
The current status matrix above is the source of truth for their disposition.

### P0 - private bearer-token policy has drifted across multiple consumers

Private route behavior is duplicated in `hestia/main.py`, `hestia/obs.py`,
`hestia/routes/web.py`, `hestia/preflight.py`, `scripts/ci-smoke.sh`, and tests. The
copies already disagree:

- `/proposal/{token}` is classified as sensitive for response headers;
- it is missing from access-log redaction, so a working proposal bearer token can be
  persisted in logs;
- it is missing from `robots.txt`;
- CI says it checks every private prefix, but its shell loop checks only a subset.

This contradicts the security playbook and changelog claims that every capability token
is redacted and every private surface is disallowed. The fix is one canonical registry
consumed by response hardening, log redaction, robots generation, and parametrized tests.

**Acceptance:** every registered capability prefix is redacted, `no-store`/`noindex`, and
robots-disallowed as appropriate; adding a new token route without registry coverage
fails CI. Add an end-to-end regression for `/proposal/<secret>` proving the secret never
appears in captured log records.

**Change path:** security-sensitive; branch + PR + human review before merge.

### P0 - migration immutability is asserted but not enforced

Migration `0065_image_access_token.sql` was edited in a later commit after it was added.
The current ledger stores only version and name, not a checksum, so two databases can
both report migration 0065 as applied while carrying different column/index definitions.
The current fresh-schema tests cannot detect that deployed-history split.

Do not edit 0065 again. Add an upgrade fixture representing the original 0065 schema,
then use a new forward migration to reconcile only what is safely reconcilable. Add a
committed migration manifest/checksum policy so future mutation fails loudly before a
deploy. Document how existing databases are inspected before any normalization.

**Acceptance:** fresh DB, current DB, and original-0065 DB upgrade paths are separately
tested; applied migration drift is observable; no historical SQL file changes.

**Change path:** live-schema/migration work; branch + PR + explicit human approval before
merge or deployment, with a timestamped DB backup first.

### P1 - the repository's own revenue-spine coverage gate is red

`scripts/coverage.sh` passes all 71 selected tests but reports 69.04%, below its 70%
threshold. The weakest measured modules are the ones with the highest commercial risk:

- `payments.py`: 44%;
- `invoices.py`: 55%;
- `vision.py`: 54%;
- `fulfillment.py`: 60%.

Add behavior-focused tests for provider failures, malformed responses, idempotent retry,
invoice transitions, and fulfillment recovery. Meet the existing 70% gate first, then
raise deliberately only when the added tests protect real invariants. Once green and
stable, make this gate required for changes to the measured spine paths.

**Acceptance:** coverage script passes at >=70%, money/idempotency failure branches are
exercised, and each new test demonstrably fails when its business invariant is broken.

### P1 - custom-domain app logic is ahead of the Caddy edge config

Hestia verifies and routes custom domains in application code, and the TLS ask endpoint
approves verified domains. The Caddyfile, however, defines only the apex and
`*.{$HESTIA_DOMAIN}` site blocks. It has no catch-all HTTPS site for customer-owned
domains, so those hosts do not reach the on-demand TLS configuration. The preflight test
only checks for the apex and wildcard strings and therefore cannot detect this gap.

Add a gated `https://` catch-all on-demand TLS block (with the existing ask endpoint),
keep the apex and hosted-subdomain blocks more specific, and validate the adapted Caddy
config. Extend preflight and tests to prove a verified external host is covered and an
unknown one is refused.

**Acceptance:** Caddy config adapts cleanly; apex, tenant subdomain, verified custom
domain, pending domain, and arbitrary domain each have an explicit test; the launch docs
describe only behavior the edge actually serves.

**Change path:** infrastructure/public-facing; branch + PR + human approval before live
deployment.

### P1 - builds are not fully reproducible or wheel-complete

The project has lower bounds but no lock/constraints file. A fresh install therefore
changes over time, including the TestClient backend now warning about removal. The Docker
image also installs the package editable from floating dependencies. Separately, the
wheel declares `static/*.css` but not the Open Graph PNG used by every base template, so
wheel installs serve a broken social preview asset.

Choose and commit one lock strategy, use it in CI and Docker, keep dependency updates
automated and reviewable, migrate the test backend before removal, include all runtime
assets, and add a clean-wheel smoke that boots the installed artifact rather than the
source tree.

**Acceptance:** locked install is repeatable; CI fails on lock drift; no deprecation
warnings; wheel contains migrations, templates, CSS, and `og-cover.png`; an installed
wheel passes `/healthz` and static-asset probes.

### P1 - release and legal metadata are not truthful yet

The changelog links `v0.1.0` and `v1.0.0`, but the Git repository has no tags. The
package declares `Proprietary` while the README says the license is TBD, and setuptools
warns that the current license-table syntax will become unsupported in 2027.

Create a small release checklist (clean gates, wheel smoke, tag, release notes, artifact)
and reconcile the version/tag history. Kevin must choose the actual license before the
license files or metadata change; this is a legal/product decision, not an autonomous
refactor.

**Acceptance:** metadata, LICENSE, README, changelog links, Git tag, and built artifact
agree for the next release.

### P2 - consolidate repeated provider and policy plumbing

The xAI-backed album, content, product, and vision modules independently repeat client
setup, authentication, JSON/error handling, and fallback behavior. After characterization
tests, extract a small shared xAI transport/result seam while leaving module prompts and
validation local. Do the same only for repeated policy, not for business logic.

Several domain modules are now 500-660 lines. Split them only along proven ownership
boundaries (query/read models vs commands/provider adapters), preserving public call
signatures and avoiding a generic repository/service layer.

**Acceptance:** fewer repeated failure paths, one place for timeout/auth/telemetry policy,
no behavioral diff, and full suite + dogfood remain green after each extraction.

### P2 - operational evidence should be automated, not prose-only

The backup, restore, preflight, and integrity stories are strong. Add scheduled evidence:

- periodic clean restore drill against a copied artifact;
- S3/local media integration smoke;
- Caddy adaptation/config validation in CI;
- release wheel/container smoke;
- alerting documentation that names the actual external monitor and owner once selected.

Do not deploy new infrastructure merely to satisfy a checklist; add only the evidence
for infrastructure Hestia actually uses.

## Original execution sequence (historical)

This sequence records the 2026-07-13 plan. It is preserved for traceability and is
superseded by the remaining sequence below.

1. **Security registry coherence** - central private-surface policy, proposal-token
   regression, complete privacy CI. PR and review required.
2. **Revenue-spine confidence** - make `scripts/coverage.sh` green with meaningful money,
   provider-failure, and idempotency tests.
3. **Packaging and dependency reproducibility** - runtime assets, lock, TestClient
   migration, clean-wheel smoke.
4. **Migration integrity design** - write the compatibility fixture and forward-only
   remediation proposal; stop for approval before schema changes.
5. **Custom-domain edge completion** - Caddy catch-all, adaptation tests, preflight; stop
   for approval before deployment.
6. **Surgical maintainability refactors** - shared provider transport and bounded module
   splits, one logical commit at a time.
7. **Release truth and launch proof** - license decision, tag/release workflow, restore and
   container evidence.
8. **Product deepening only after the quality floor** - validate the live vision path and
   style-profile behavior against real photography workflows; do not add broad new modules.

## Remaining execution sequence

1. ✅ **Live-vision resilience (GREEN)** - response bytes and result fields are bounded;
   one clearly labeled whole-gallery mock fallback preserves offer creation and token
   idempotency when xAI fails, while reprocess retries live vision.
2. ✅ **Vision calibration snapshot (GREEN)** - export one safe review row per image with
   current inputs, decisions, weak labels, and blank reviewer columns. Historical run
   provenance, a paid API benchmark, or customer photography remain separately human-gated.
3. ✅ **Storage footprint visibility (GREEN)** - tenant-scoped byte totals and operator
   rollups now use existing metadata with anomaly counts and explicit thumbnail,
   generated-render, and provider-cost exclusions. No quota or dollar estimate is
   enforced without a pricing decision.
4. **Product and risk decisions** - obtain human decisions for AI subsidy/BYOK
   disclosure, media-token scope, Stripe settlement semantics, fulfillment depth,
   timezone/calendar schema, migration integrity, and the public edge.
5. **Approved corrections** - implement money, security, schema, and infrastructure
   changes only inside their approved designs and with their stronger gates.
6. **Release truth** - reconcile license, tags, and release metadata only after the
   owner chooses the actual legal and packaging posture.

## Autonomous working contract

For each logical change:

1. Re-read the affected exports, callers, SQL, templates, and tests.
2. State the invariant and a test that can fail when it breaks.
3. Make the smallest feature-preserving change.
4. Run targeted tests, then `scripts/ci-smoke.sh`; run `scripts/coverage.sh` for revenue
   spine changes and `scripts/dogfood-hestia.sh` for workflow changes.
5. Commit one coherent change with its verification evidence.
6. Use direct `main` only for safe, verified work. Security, money/legal, schema, and
   infrastructure changes use a branch/PR and wait at the human gate.
7. Do not deploy, delete remote branches/tags, or rewrite shared state without approval.

The order can change only when new evidence changes risk. Scope does not expand into a
rewrite, a microservice split, or feature deletion.

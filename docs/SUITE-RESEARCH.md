# Suite research — architecture & product memo

**Date:** 2026-06-25
**Method:** Cloned and read the actual code of all six photography repos
(`mise`, `argus`, `plutus`, `mnemosyne`, `dionysus`, `aphrodite`) — ~81k LOC
total — and extracted real API contracts, auth, data models, and maturity. This
memo supersedes assumptions in `docs/SUITE.md`, which is a map, not ground truth.

> **Bottom line:** A full greenfield rewrite would destroy real value — these are
> mature, tested, mostly-deployed services with genuinely differentiated engines.
> But a *pure* thin orchestrator under-delivers too, because **the magic-moment
> loop already works without Hestia**, and the services are multi-tenant in three
> incompatible ways while Mise isn't multi-tenant at all. The defensible answer is
> a **hybrid**: build Hestia as the unified identity/tenancy/billing/UI control
> plane the suite lacks, **orchestrate** the three mature engines (Argus, Plutus,
> Mnemosyne) over HTTP, and **re-platform (not wrap) Mise's gallery core** for
> multi-tenancy. Fold Dionysus in; treat Aphrodite as a separate product.

---

## 1. Maturity & deployment (all six are real)

| Service | LOC | Tests | Deploy | Tenancy | Verdict |
|---------|----:|-------|--------|---------|---------|
| **mise** | 27k | 210 test fns | systemd, CI, **prod @ kleephotography.com** | **Single-tenant** (one admin pw, no tenant tables) | Mature monolith; the gallery source of truth |
| **plutus** | 16k | 47 files, Postgres CI | Dockerfile + **fly.toml** | **Multi-tenant** (`tenants`, `plutus_tk_…`) | Most production-shaped service |
| **argus** | 13k | 169 test fns | systemd, CI | **Multi-tenant** SaaS mode (`argus_tk_…`) | Mature; already orchestrates internally |
| **mnemosyne** | 8k | 27 files | Dockerfile + **fly.toml** (on Fly) | Cookie users (no API token) | Mature; distinct album engine |
| **aphrodite** | 10.6k | 15 files, CI | **no deploy manifest**, v0.1.0 | Dual bearer tokens | Real, but **different market** |
| **dionysus** | 6k | 4 files | systemd, **no CI/Docker** | Own `organizations` model | Least mature; **"AI" not wired in** |

None of these are throwaway experiments. The cheap-to-rebuild assumption behind
"just make a new product" does not hold.

---

## 2. The most important finding: the loop already works

The magic moment — *publish a gallery → vision → client-ready Plutus offer URL* —
**is already implemented and runs today**, with no Hestia:

- **Mise auto-fires on publish** (`mise/app/admin/galleries.py:401`): a published
  gallery enqueues `argus_analyze_gallery`; Argus's completion callback chains to
  Plutus.
- **Argus already orchestrates the whole chain**: `argus` exposes
  `POST /ui/pipeline/run-all/{gallery_id}` and an internal `pipeline.py run_all`
  that goes gallery → vision → Plutus offer and returns the `offer_url`.
- **The reference dogfood** (`plutus/scripts/dogfood_suite_loop.py`) proves it:
  its default path is a single `POST {argus}/ui/pipeline/run-all/{gallery_id}`,
  which surfaces Plutus's `offer_url` from a 303 redirect.

**Implication:** Hestia's value proposition cannot be "orchestrate the loop" — the
loop exists. The thing **no service owns** is a *unified, multi-studio control
plane*: one signup, one tenant identity propagated across services, one bill, one
dashboard, one place to send the client. That — not orchestration — is the gap.

---

## 3. Real critical-path contracts (corrected vs docs)

The actual wiring, gallery → offer:

1. **Mise → Argus** (`mise/app/argus_analyze.py:108`):
   `POST {ARGUS}/analyze-folder`, `Authorization: Bearer {MISE_ARGUS_TOKEN}`,
   **form-urlencoded** `mise_gallery_id=<int>, limit=20, source=mise,
   callback_url={MISE_BASE_URL}/api/argus/callback?gallery_id=<id>`.
   Identifier is the **numeric gallery id**. **Argus reads the JPEGs itself off a
   shared local disk** (`ARGUS_MISE_MEDIA_ROOT`) — pixels are never uploaded.
2. **Argus analyze** (`argus/app/main.py:379`): `POST /analyze-folder` is
   **multipart form**, and when `ARGUS_QUEUE_ENABLED` (default) it is **async** —
   returns `{mode:"queued", job_id}`; poll `GET /jobs/{job_id}` → `run_id`.
   Per-image result: `shot_type`, `keywords[]`, `culling{keeper_score,
   hero_potential, technical_quality}`. Vision backend = **xAI Grok** (not local).
3. **Argus/Mise → Plutus**: either `POST {PLUTUS}/recommend/mise-gallery`
   (`mise/app/plutus_recommend.py:60`, body `mise_gallery_id`) **or** the
   canonical automation hook `POST {PLUTUS}/webhooks/mise/gallery-published`
   (Bearer **`PLUTUS_MISE_HOOK_TOKEN`** — a *separate* token), which with
   `PLUTUS_MISE_AUTO_OFFER=true` recommends **and auto-mints the offer**.
4. **Plutus offer mint** (`plutus/app/routes/integrations.py`):
   `POST /integrations/offer`, **form field `run_id` (a recommendation run id —
   NOT a gallery id)**, `Authorization: Bearer plutus_tk_<tenant_id>_<hex>` (or
   admin `PLUTUS_API_TOKEN` + `tenant_id`). Response: offer URL is the
   **`public_url`** field (`/store/<slug>/offer/<token>`).
   **⚠ No idempotency** (`plutus/app/storefront.py:46`): every call `INSERT`s a
   fresh `token_urlsafe(24)` — re-minting on the same run **duplicates the offer**.
5. **Minting does NOT require Stripe.** `create_share_link` never touches billing;
   Stripe only gates the final client checkout. The loop is *not* blocked by Stripe
   being unconfigured.

**Doc-drift corrected:** Plutus keys on `run_id` not `gallery_id`; Argus analyze is
multipart + async job-queue, not a simple JSON POST; Mnemosyne is **cookie-only,
no API token**; the Plutus Mise-hook token is distinct from the admin token.

---

## 4. The reference E2E (transcribed from `dogfood_suite_loop.py`)

1. Health gate: `GET` `argus:8010/healthz`, `plutus:8031/healthz`, `mnemosyne:8000/healthz`.
2. Trigger: `POST {argus}/ui/pipeline/run-all/{gallery_id}` (form `api_token`) → 303,
   parse `offer_url` from redirect query. *(Argus internally calls Plutus's hook.)*
3. Verify: `GET <offer_url>` asserts storefront renders (`package|bundle|buy`).
4. Mnemosyne attach: `POST /login` (cookie) → `POST /albums/{id}/plutus-generate`
   (form `plutus_run_id`) → `POST /albums/{id}/share` → `GET /share/{token}`
   asserts an **"Order prints"** CTA.

**Three different auth schemes in one loop**: Argus bearer token, Plutus hook
token, Mnemosyne cookie login. A unifying shell has to broker all three.

---

## 5. Auth & ports reality

| Service | Port (home/cloud) | Auth scheme | Token / secret |
|---------|------|-------------|-------|
| mise | 8400 | single admin password; bearer for `/api/*` | `MISE_ADMIN_PASSWORD`, `MISE_ARGUS_TOKEN` |
| argus | 8010 / 8020 | bearer (open if unset on homelab) | `ARGUS_API_TOKEN`, `argus_tk_<t>_<hex>` |
| plutus | 8030 / 8031 | bearer; separate Mise-hook token | `plutus_tk_<t>_<hex>`, `PLUTUS_MISE_HOOK_TOKEN` |
| mnemosyne | 8000 | **cookie session only** | (none — `POST /login`) |
| dionysus | 8450 | bearer for `/api/mise/*` | `DIONYSUS_MISE_IMPORT_TOKEN` |
| aphrodite | **8020** ⚠ | dual bearer | `APHRODITE_API_TOKEN`, `_WORKER_TOKEN` |

⚠ **Aphrodite's default port 8020 collides with Argus's cloud port 8020.**

---

## 6. Duplication analysis (the consolidation lens)

What's genuinely *differentiated* (the moat — keep it):

- **Argus**: photography-tuned vision (keywording, culling/hero scoring) via Grok.
- **Plutus**: print/album catalog, Stripe checkout, WHCC lab adapter, storefront.
- **Mnemosyne**: LLM album-spread arrangement with a deterministic placement
  guardrail ("model judges, code validates") + PDF export.
- **Mise**: gallery delivery + a full HoneyBook-style CRM/invoicing/CMS.

What's **duplicated 4–6×** (the real consolidation prize — lift into the shell):

- **Identity / tenancy**: six separate stores — Mise (one admin pw), Plutus
  `tenants`, Argus `tenants`, Mnemosyne `users`, Dionysus `organizations`,
  Aphrodite tokens. Same concept, six schemas, three+ auth schemes.
- **Billing/Stripe**: Mise (client invoices) and Plutus (print checkout) each carry
  full Stripe customer + webhook plumbing; Mnemosyne and Dionysus have their own too.
- **Storage**: every service re-ingests or re-reads gallery media; three of them
  read **the same originals off a shared local disk keyed by Mise's gallery id.**
- **Share links / "order prints"**: Plutus *and* Mnemosyne both mint public share
  URLs that gesture at print ordering — overlapping surfaces.

---

## 7. Two structural problems a SaaS must confront (not paper over)

1. **Tenancy mismatch.** Plutus/Argus are multi-tenant; **Mise is hard
   single-tenant** — one admin password, zero tenant columns across 60 migrations,
   integer gallery ids tied to local disk. "Hestia orchestrates Mise per studio"
   is **not viable** without rewriting Mise's auth, storage (id collisions across
   studios), and the filesystem-coupled Argus path. A real multi-tenant SaaS
   **absorbs/re-platforms Mise's gallery core**, keeping Argus/Plutus/Mnemosyne as
   services.
2. **Shared-disk coupling.** Argus, Plutus, and Mnemosyne read original pixels off
   a shared local media root, not over HTTP. That's a homelab-fleet assumption;
   across Fly/cloud hosts it breaks. Plutus and Mnemosyne already support S3/R2;
   Argus optionally. Moving to shared **object storage** is a real, necessary
   migration cost for SaaS.

---

## 8. Per-service recommendation

| Service | Call | Why |
|---------|------|-----|
| **Argus** | **Orchestrate** | Mature, clean HTTP seam, already the suite hub. Don't absorb its Grok client + job worker + SaaS layer. |
| **Plutus** | **Orchestrate** | Most production-shaped; clean webhook-in / offer-out seams. Shell must add the idempotency Plutus lacks. |
| **Mnemosyne** | **Orchestrate** | Distinct album engine; loosely coupled already. Delegate its print CTA to Plutus instead of half-duplicating. |
| **Mise** | **Re-platform / absorb core** | Single-tenant + shared-disk make it the consolidation target, not a satellite. Multi-tenant galleries + object storage is a rebuild of *its tenancy/storage layers*, not a wrapper. |
| **Dionysus** | **Absorb (or shelve)** | Least mature; its headline "AI" is deterministic templates with no LLM. Unique asset ≈ 200 lines. Fold into the shell, wire to a shared LLM later. |
| **Aphrodite** | **Separate product** | Different market (e-commerce merchants, not photographers), zero suite integration, distinct nouns (packshots/SKUs). Shares the framework, not the customer. |

---

## 9. So: orchestrate, consolidate, or new product?

- **Full greenfield rewrite — no.** It throws away ~81k LOC of tested, deployed,
  differentiated engines. The vision, print/Stripe/WHCC, and album-arrangement
  logic are the moat; rebuilding them is months of negative-value work.
- **Pure thin orchestrator — not enough.** The loop already works without a shell,
  the services are multi-tenant three different ways, Mise isn't multi-tenant, and
  shared-disk breaks across hosts. A shell that only proxies adds little.
- **Hybrid — yes.** Build Hestia as the **control plane the suite is missing**:
  unified multi-tenant identity + tenancy propagation, one billing surface, one UI,
  and an **idempotent** broker over the *existing* loop. **Orchestrate** Argus,
  Plutus, Mnemosyne. **Re-platform Mise's gallery/identity core** for multi-tenancy
  (this is the part that's genuinely a "new product"). **Absorb** Dionysus.
  **Spin out** Aphrodite.

This honors the original instinct — the *gallery + identity core* really does want
consolidation — without discarding the mature engines that make the suite valuable.

---

## 10. Open questions for Kevin

1. **Studio count for v1**: one studio (yours), or true multi-tenant SaaS from day
   one? This is the fork: single-studio → orchestrate as-is (loop already works,
   Hestia = unified UI). Multi-tenant → re-platform Mise's core first.
2. **Mise's future**: are you willing to re-platform Mise's galleries onto
   multi-tenant + object storage, or should Mise stay your single-studio engine and
   Hestia onboard *other* studios with a fresh gallery core?
3. **Dionysus**: wire a real LLM and keep it, or shelve until the suite has paying
   studios?
4. **Aphrodite**: confirm it's a separate product line (recommended), not a Hestia
   module.

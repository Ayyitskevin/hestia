# Hestia

**The AI-native studio OS for photographers.** Book, deliver, sell, invoice, and
retain — from one warm, intelligent studio command center.

---

## The pitch

Photographers run their business across a booking tool, a gallery host, a separate
invoicing app, a spreadsheet CRM, and hours of manual culling. Hestia replaces that
stack with **one multi-tenant SaaS** that owns the whole studio revenue loop — public
inquiry to paid client — and embeds AI exactly where it saves time or makes money.
One login, one database, one bill. Not an AI photo toy; the **client-to-cash
operating system** for a studio.

> Canonical product definition: **[`docs/HESTIA-DOCTRINE.md`](docs/HESTIA-DOCTRINE.md)**.
> Building on Hestia (human or agent)? Read **[`AGENTS.md`](AGENTS.md)** first.

## The magic moment

A photographer uploads a gallery and within seconds has a **client-ready offer URL** —
print and album bundles curated from the gallery's own AI vision signal, behind one
shareable link that **never duplicates on re-run**.

```text
$ bash scripts/dogfood-hestia.sh
🔥 MAGIC MOMENT
   offer URL : http://127.0.0.1:8590/s/dogfood-studio/EEUeZqZsBbyqR2XdjviNpVDvB7bX
   vision→offer in 0.5s
   idempotent: re-process produced the same link ✓
```

## Who it's for

A working **solo or small-studio photographer** — weddings, portraits, events,
commercial/product — who wants to stop stitching tools together and run the studio
from one place. The buyer and the daily user are the same person, so it's personal
and warm, not enterprise. Roles and oversight scale it to a **small team/agency**.

## Core workflow

Every feature hangs off one spine — public to paid to repeat:

```text
visitor → inquiry → client → project → gallery
  → AI culling / metadata → offer → album draft → marketing pack
  → invoice → payment → retention / upsell
```

## Modules

One product, several modules sharing one tenant/client/project/gallery spine — **not**
six services in a trench coat.

| Module | Owns |
|--------|------|
| `studio` | public studio site + inquiry → CRM lead |
| `crm` | clients + projects — the studio backbone |
| `galleries` · `storage` | native gallery + image hosting (`local`/`s3` seam) |
| `vision` | cull / keyword / hero scoring (`mock`/`xai`) |
| `sales` | print/album/product **idempotent** offer engine |
| `albums` | drafted album spreads — model proposes, **code validates** |
| `content` | shot lists, captions, campaign copy (`mock`/`xai`) |
| `products` | marketplace-spec packshot variants (`mock`/`xai`) |
| `invoices` · `payments` | invoicing + checkout (`mock`/`stripe`) |
| `subscriptions` · `billing` | studio plans + billing (`mock`/`stripe`) |
| `pipeline` · `jobs` | gallery automation on a durable job queue |

## Why Hestia wins

- **It owns the workflow, not just a tool.** Whoever owns book→deliver→sell→retain
  owns the customer; point tools get swapped out.
- **AI where it compounds.** Culling, hero picks, offer curation, album drafts, and
  marketing copy at the exact revenue/admin friction points — model proposes, code
  validates.
- **Idempotent money paths.** One offer link per gallery, reused forever; no
  duplicate client links, no double-settled invoices.
- **SaaS-native from the foundation.** Every row is tenant-scoped; every studio has
  users, roles, plan/billing, a public profile, and a storage namespace.
- **Warm, photographer-native UX** — a studio hearth, not generic purple SaaS.

## Quickstart

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # set real secrets; chmod 600 .env
bash scripts/start-hestia.sh    # → http://127.0.0.1:8500
```

- `/` landing · `/admin` (master `HESTIA_API_TOKEN`) onboards a studio
- `/login` → dashboard → clients · projects · galleries · invoices · site · billing
- `/studio/{slug}` the studio's public page · `/healthz` liveness · `/readyz` readiness

## Mock-first provider seams

Everything runs with **no external keys** by default. Each integration is a seam that
flips to a real provider independently — add a backend, don't fork the caller:

| Seam | Env | Default | Real |
|------|-----|---------|------|
| Vision / Album / Content / Product | `HESTIA_*_BACKEND` | `mock` | `xai` (+ `HESTIA_XAI_API_KEY`) |
| Storage | `HESTIA_STORAGE_BACKEND` | `local` | `s3` (S3/R2/MinIO) |
| Payments | `HESTIA_PAYMENTS_BACKEND` | `mock` | `stripe` |
| Subscriptions | `HESTIA_SUBSCRIPTION_BACKEND` | `mock` | `stripe` |
| Email | `HESTIA_EMAIL_BACKEND` | `mock` (outbox) | `smtp` |

A real backend that errors **degrades to the safe path** — it never 500s a request.
Boot logs warn loudly if a real backend is selected without its keys.

## SaaS architecture

A **modular monolith**: FastAPI + Jinja2 + HTMX + SQLite (WAL). One tenant model, one
auth/session/API-key system, one billing model, one audit trail, one storage
abstraction, many modules. A durable SQLite-backed **job queue** (retries, crash
reclaim) runs the pipeline off the request thread. Schema is forward-only numbered
migrations applied via a ledger. Structured JSON logging with per-request ids.
Operator surfaces: `/readyz`, `/admin/system` (queue depth, seam modes, migrations).
Details: [`docs/architecture.md`](docs/architecture.md).

## Tests, CI, dogfood

```bash
bash scripts/ci-smoke.sh        # ruff + pytest + /healthz boot
bash scripts/dogfood-hestia.sh  # boot the app, drive the magic moment, assert an offer
```

CI runs both on every push ([`.github/workflows/test.yml`](.github/workflows/test.yml)).
Tested invariants include tenant isolation, offer idempotency, and safe mock-provider
operation with no keys.

## Roadmap

Sequenced from a verified competitive analysis — full landscape, wedge, pricing, and
the phased build list in **[`docs/COMPETITIVE-STRATEGY.md`](docs/COMPETITIVE-STRATEGY.md)**.

- **Now:** the full delivery→paid loop works end to end (inquiry → paid), mock-first.
- **Phase 1 — contract-to-cash credibility:** contracts + e-signature, payment plans,
  client portal, questionnaires (the booking-side table stakes).
- **Phase 2 — scheduling + automations:** self-booking calendar, event-triggered workflows.
- **Phase 3 — defend the after-the-shoot loop:** gallery proofing/favorites, AI-curated
  sales-automation campaigns, print-store fulfillment.
- **Phase 4 — AI compounding + retention:** vision parity + studio AI profiles, upsell
  automations, mobile surfaces.

## Status

| Item | State |
|------|-------|
| Core loop | public site · CRM · galleries · vision · offers · albums · marketing · products · invoicing · subscriptions |
| AI seams | `mock` (default) or xAI Grok |
| Payments / Subscriptions | `mock` (default) or Stripe (+ webhook) |
| Storage | local filesystem (`local`/`s3` seam) |
| Platform | durable job queue · migrations · structured logging · readiness/ops surfaces |
| Signup | gated (`HESTIA_SIGNUP_ENABLED=false`) — admin onboarding by default |

## License

TBD — under active development by [Kevin Lee](https://github.com/Ayyitskevin).

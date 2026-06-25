# Hestia

**The AI-native studio OS for photographers — run your whole studio from one place.**

Clients and projects, gallery delivery, AI culling and keywording, print &
album sales, invoicing and payments — one multi-tenant app, one login, one bill.

> **For AI agents:** read [`AGENTS.md`](AGENTS.md) first, then
> [`docs/BEHEMOTH.md`](docs/BEHEMOTH.md) (the module roadmap) and
> [`docs/PHASE-0.md`](docs/PHASE-0.md). The product is a single multi-tenant app —
> modules, not microservices. Why we consolidated instead of orchestrating six
> separate services is documented, with evidence, in
> [`docs/SUITE-RESEARCH.md`](docs/SUITE-RESEARCH.md).

---

## What this is

Hestia distills the best of a six-project photography suite (a studio OS, a vision
API, a print/album sales layer, an album designer, and two adjacent bets) into
**one coherent product**. Instead of six services each reimplementing identity,
billing, and storage and talking over HTTP, Hestia is a single FastAPI + HTMX +
SQLite app with every capability as an in-process module.

The studio's real workflow, end to end:

```text
client → project → gallery → AI vision (cull · keyword · heroes)
                          → print & album offer → client buys
                 → invoice → paid
```

---

## The magic moment

A photographer uploads a gallery and within seconds has a **client-ready offer
URL** — print and album bundles curated from the gallery's own vision signal,
behind one shareable link that never duplicates on re-run.

```text
$ bash scripts/dogfood-hestia.sh
🔥 MAGIC MOMENT
   offer URL : http://127.0.0.1:8590/s/dogfood-studio/EEUeZqZsBbyqR2XdjviNpVDvB7bX
   vision→offer in 0.5s
   idempotent: re-process produced the same link ✓
```

---

## Modules (one app, not microservices)

| Module | What it does | Best-of |
|--------|--------------|---------|
| `tenants.py` · `auth.py` | multi-tenant studios, users, API keys | (control plane) |
| `crm.py` | clients + projects — the studio backbone | Mise back-office |
| `galleries.py` · `storage.py` | native gallery + image hosting (S3-ready seam) | Mise delivery |
| `vision.py` | cull / keyword / hero scoring (pluggable: `mock`/`xai`) | Argus |
| `sales.py` | print/album bundles + **idempotent** client offers | Plutus |
| `invoices.py` · `payments.py` | invoicing + checkout (pluggable: `mock`/`stripe`) | Mise + Plutus |
| `pipeline.py` | gallery → vision → offer (persisted, live stepper) | the dogfood loop |

**Roadmap** ([`docs/BEHEMOTH.md`](docs/BEHEMOTH.md)): ✅ CRM · ✅ Invoicing &
payments · next: album designer (Mnemosyne) → marketing content (Dionysus) →
product photography (Aphrodite) → public studio site.

---

## Quickstart

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # set real secrets; chmod 600 .env
bash scripts/start-hestia.sh    # → http://127.0.0.1:8500
```

- `/` landing · `/admin` (master `HESTIA_API_TOKEN`) onboards a studio
- `/login` → dashboard → clients · projects · galleries · invoices
- `/healthz` liveness + self checks

Everything runs with no external keys by default: vision is `mock` (deterministic)
and payments are `mock` (simulated checkout). Set `HESTIA_VISION_BACKEND=xai` +
`HESTIA_XAI_API_KEY` for live Grok vision, and `HESTIA_PAYMENTS_BACKEND=stripe` +
`HESTIA_STRIPE_SECRET_KEY` for real checkout.

---

## Why one app and not an orchestrator?

This repo began as a shell to orchestrate six existing services over HTTP. Reading
the actual code of all six changed the plan: they are mature and deployed, but they
reimplement identity, billing, and storage six times over, and the gallery→offer
loop already worked *without* a shell. The duplication — not the orchestration — was
the real problem. So Hestia **consolidates** the essence into one product and keeps
the differentiated engines (vision, sales, albums) as modules. Full evidence,
including the real (and corrected) service contracts, is in
[`docs/SUITE-RESEARCH.md`](docs/SUITE-RESEARCH.md). One example: the real Plutus
mints a *new* client link on every call (no idempotency) — Hestia fixes that by
design (one offer/token per gallery, reused on every re-run).

---

## Shoot-type presets

Same product for every studio; shoot type tunes offer/album defaults
([`hestia/features.py`](hestia/features.py)).

| `shoot_type` | Album bundle | Hero picks |
|--------------|--------------|-----------|
| wedding · event · portrait | yes | 8 / 6 / 5 |
| commercial · food · other | no | 5 |

---

## Tests, CI, dogfood

```bash
bash scripts/ci-smoke.sh        # ruff + pytest + /healthz boot
bash scripts/dogfood-hestia.sh  # boot the app, drive the magic moment, assert an offer
```

CI runs both on every push ([`.github/workflows/test.yml`](.github/workflows/test.yml)).

---

## Status

| Item | State |
|------|-------|
| Shipped | studios · CRM · galleries · vision · sales offers · invoicing & payments |
| Vision | `mock` (default) or xAI Grok |
| Payments | `mock` (default) or Stripe |
| Storage | local filesystem — S3/R2 next |
| Signup | invite-only (`HESTIA_SIGNUP_ENABLED=false`) |

---

## License

TBD — under active development by [Kevin Lee](https://github.com/Ayyitskevin).

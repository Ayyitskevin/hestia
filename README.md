# Hestia

**The AI-native studio for photographers — gallery to paid, in one app.**

Deliver galleries, let AI understand every frame, and turn each gallery into print
& album revenue — one login, one pipeline, one bill. No fleet of services to wire.

> **For AI agents:** read [`AGENTS.md`](AGENTS.md) first, then
> [`docs/PHASE-0.md`](docs/PHASE-0.md). The product is a single multi-tenant app
> (modules, not microservices). The decision to consolidate rather than orchestrate
> is documented — with evidence — in [`docs/SUITE-RESEARCH.md`](docs/SUITE-RESEARCH.md).

---

## What this is

Hestia distills the essence of a six-project photography suite (a studio OS, a
vision API, a print/album sales layer, an album designer, and two adjacent bets)
into **one coherent product**. Instead of six services duplicating identity,
billing, and storage six times over and talking over HTTP, Hestia is a single
FastAPI + HTMX + SQLite app with the AI engines as in-process modules.

The core loop:

```text
Upload gallery → AI vision (cull · keyword · pick heroes)
              → auto-built print & album offer → shareable client link → client buys
```

That whole loop is a function call, not a network of services — which is the point.

---

## The magic moment

A photographer uploads a gallery and within seconds has a **client-ready offer
URL** — print and album bundles curated from the gallery's own vision signal,
behind one shareable link. Re-process all you like: the link never duplicates.

```text
$ bash scripts/dogfood-hestia.sh
🔥 MAGIC MOMENT
   offer URL : http://127.0.0.1:8590/s/dogfood-studio/EEUeZqZsBbyqR2XdjviNpVDvB7bX
   vision→offer in 0.5s
   idempotent: re-process produced the same link ✓
```

---

## Quickstart

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # set real secrets; chmod 600 .env
bash scripts/start-hestia.sh    # → http://127.0.0.1:8500
```

- `/` — landing
- `/admin` — admin (master `HESTIA_API_TOKEN`) → onboard a studio
- `/login` — studio owner → dashboard → galleries → process → offer
- `/healthz` — liveness + self checks

With `HESTIA_VISION_BACKEND=mock` (default) everything runs with no API key.
Set `HESTIA_VISION_BACKEND=xai` + `HESTIA_XAI_API_KEY` for live Grok vision.

---

## Architecture (one app, modules not microservices)

```text
hestia/
  main.py config.py db.py auth.py crypto.py
  tenants.py            # studios + users + API keys
  features.py           # shoot-type presets → offer/album tuning
  storage.py            # object storage (local now, S3/R2 in Phase 1)
  galleries.py          # native multi-tenant gallery + image hosting
  vision.py             # AI vision module (mock | xAI Grok)  ← essence of Argus
  sales.py              # offer builder + idempotent client offers  ← essence of Plutus
  pipeline.py           # gallery → vision → offer (persisted, idempotent)
  billing.py            # plan scaffold (live Stripe = Phase 1)
  routes/  templates/  static/
```

| Concern | Where |
|---------|-------|
| Multi-tenant studios, auth, API keys | `tenants.py`, `auth.py` |
| Native galleries + images | `galleries.py` + `storage.py` |
| Understand every frame | `vision.py` (pluggable provider) |
| Turn galleries into revenue | `sales.py` (idempotent offers) |
| Run state + live stepper | `pipeline.py` |

See [`docs/architecture.md`](docs/architecture.md) for the data flow and diagram.

---

## Why one app and not an orchestrator?

This repo began as a shell to orchestrate six existing services over HTTP. Reading
the actual code of all six changed the plan: they are mature and deployed, but they
reimplement identity, billing, and storage six times, and the gallery→offer loop
already worked *without* a shell. The duplication — not the orchestration — was the
real problem. So Hestia **consolidates** the essence into one product and keeps the
differentiated engines (vision, sales, albums) as modules. The full evidence,
including the real (and corrected) service contracts, is in
[`docs/SUITE-RESEARCH.md`](docs/SUITE-RESEARCH.md).

Notably, the real Plutus mints a **new** client link on every call (no idempotency).
Hestia fixes that by design: one offer/token per gallery, reused on every re-run.

---

## Shoot-type presets

Same product for every studio; shoot type tunes offer/album defaults
([`hestia/features.py`](hestia/features.py)).

| `shoot_type` | Album bundle | Hero picks |
|--------------|--------------|-----------|
| wedding | yes | 8 |
| event | yes | 6 |
| portrait | yes | 5 |
| commercial | no | 5 |
| food | no | 5 |
| other | no | 5 |

---

## Tests, CI, dogfood

```bash
bash scripts/ci-smoke.sh        # ruff + pytest + /healthz boot
bash scripts/dogfood-hestia.sh  # boot the app, drive the magic moment, assert an offer
```

CI runs both on every push ([`.github/workflows/test.yml`](.github/workflows/test.yml)).

---

## Status & phase

| Item | State |
|------|-------|
| Phase | **0** — prove the magic moment in one app |
| Signup | Invite-only (`HESTIA_SIGNUP_ENABLED=false`) |
| Vision | mock (default) or xAI Grok |
| Billing | scaffold — live Stripe is Phase 1 |
| Storage | local filesystem — S3/R2 is Phase 1 |

Phase 1: live Stripe checkout on offers, S3/R2 storage, public signup, and the
album-design module (the essence of Mnemosyne) — see [`docs/PHASE-1.md`](docs/PHASE-1.md).

---

## License

TBD — under active development by [Kevin Lee](https://github.com/Ayyitskevin).

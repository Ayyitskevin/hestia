# Hestia

**Everything you need to run a professional photography studio - fully hosted,
AI-powered, and maintained for you, only $40/month.**

Hestia is the all-in-one command center for growing photography studios. It brings
inquiry, booking, contracts, questionnaires, galleries, AI culling/offers, invoices,
payments, fulfillment, and retention into one lightweight hosted product.

**14-day free trial. One flat paid plan. Cancel anytime. No tiers.**

> Product doctrine: [`docs/HESTIA-DOCTRINE.md`](docs/HESTIA-DOCTRINE.md)
> Strategy and wedge: [`docs/COMPETITIVE-STRATEGY.md`](docs/COMPETITIVE-STRATEGY.md)
> Agents and contributors: read [`AGENTS.md`](AGENTS.md) first.

---

## The Offer

**Hestia Studio - $40/month**

For less than the cost of one typical gallery or booking tool, a studio gets the
full hosted operating system:

| Replaces | Hestia includes |
|----------|-----------------|
| Booking scheduler | Public booking pages, availability rules, confirmations, reminders |
| CRM spreadsheet | Clients, projects, timelines, tags, notes, files, statements |
| Proposal app | Package-backed proposals with view tracking, automated follow-up nudges, agreement, and deposit |
| Contract tool | Templates, sent agreements, typed e-signature, audit trail |
| Intake forms | Questionnaires, reusable templates, client portal responses |
| Gallery host | Native galleries, delivery links, proofing, favorites, comments |
| AI helper apps | Culling, hero picks, metadata, offer curation, album drafts, copy |
| Invoicing app | Invoices, payment plans, deposits, receipts, Stripe checkout |
| Retention chores | Review asks, rebooking reminders, reconnect surfaces, owner digest |

The promise is simple: **one login, one bill, one maintained studio command center.**

## Who It Is For

Hestia is built for solo and small-studio photographers who are past "just taking
photos" and need a professional workflow without stitching together 5-7 separate
apps.

The first hosted presets target:

- **Wedding photographers** who need inquiries, consultations, contracts, deposits,
  galleries, albums, sales, and anniversary retention.
- **Food & beverage photographers** who need menu launches, campaign days,
  recurring content retainers, licensing-friendly intake, and repeat client loops.
- **Real-estate photographers** who need fast booking, property intake, delivery,
  invoice collection, and broker/agent rebooking.

## The Customer Journey

```text
signup -> email verification -> onboarding preset -> trial cockpit
  -> public studio site -> inquiry/booking -> proposal -> contract + deposit
  -> questionnaire -> shoot -> gallery -> AI offer -> order/payment
  -> fulfillment -> review/rebooking/retention
```

Every step is tenant-scoped and built into the same FastAPI/Jinja/HTMX app.

## The Magic Moment

A photographer uploads a gallery and gets a client-ready offer URL within seconds.
The offer is generated from the gallery's AI vision signal and is idempotent, so
re-processing never creates duplicate client links.

```bash
bash scripts/dogfood-hestia.sh
```

Example output:

```text
MAGIC MOMENT
   offer URL : http://127.0.0.1:8590/s/dogfood-studio/...
   vision->offer in 0.4s
   idempotent: re-process produced the same link
```

## Hosted SaaS Mode

Hestia is ready for a simple solo-founder hosted launch:

- Dockerized FastAPI app
- Caddy reverse proxy
- SQLite WAL on a persistent volume
- Tenant isolation through shared tenant IDs and storage namespaces
- Optional wildcard studio subdomains
- Stripe subscriptions locked to the flat $40/month plan
- Operator trial conversion cockpit for activation and churn-risk signals
- First-party signup attribution from the public demo and pricing pages
- Mock-first provider seams for safe local and staging runs

```bash
cp .env.example .env
# set HESTIA_DOMAIN, HESTIA_PUBLIC_URL, HESTIA_* secrets, Stripe/SMTP/S3 keys as needed
docker compose up --build -d
```

Set wildcard DNS for `*.yourdomain.com` to the host running Caddy. With
`HESTIA_DOMAIN=yourdomain.com`, a studio with slug `oak-room` is reachable at:

```text
https://oak-room.yourdomain.com
https://yourdomain.com/studio/oak-room
```

Custom domains are prepared from `/settings/account` with a CNAME target and TXT
verification token. Operators verify DNS from the admin tenant detail page before
the custom domain routes publicly.

## Trial, Billing, And Account Flow

Hosted customers move through the product with minimal setup:

1. `/signup` creates the studio and owner account.
2. Email verification signs the owner in and opens `/onboarding`.
3. `/onboarding` installs the wedding, food & beverage, or real-estate preset.
4. `/dashboard` shows the hosted studio cockpit and next launch action.
5. `/settings/billing` starts the 14-day trial.
6. `/settings/account` shows studio URLs, custom-domain readiness, and billing actions.

The billing implementation intentionally exposes only one paid plan:

```text
Hestia Studio
14-day free trial
$40/month after trial
Cancel anytime
No paid tiers
```

In mock mode, billing is deterministic and local. With
`HESTIA_SUBSCRIPTION_BACKEND=stripe`, checkout and billing portal sessions use
Stripe while keeping the app logic behind the same subscription seam.

## Modules Included

Hestia is a modular monolith, not a bundle of disconnected services.

| Module | Owns |
|--------|------|
| `studio` | Public studio site and inquiries |
| `crm` | Clients, projects, tags, files, timelines |
| `scheduler` | Availability, booking, confirmations, reminders |
| `proposals` | Package-backed proposals, accept flow, automated nudges, linked agreement + deposit invoice |
| `dashboard` | Trial cockpit, launch path, proposal conversion analytics, digest-ready attention queue |
| `contracts` | Contract templates, sent agreements, e-signature |
| `questionnaires` | Intake forms, answers, reusable templates |
| `galleries` / `storage` | Gallery hosting, uploads, delivery |
| `proofing` | Favorites, comments, client selections |
| `vision` | Culling, keywords, hero scoring, AI signal |
| `sales` / `campaigns` | AI-curated offers, sales campaigns, offer links |
| `orders` / `fulfillment` | Paid orders and print-lab seam |
| `albums` | Draft album spreads with code validation |
| `content` / `products` | Captions, shot lists, marketing packs, packshots |
| `invoices` / `payments` | Invoices, deposits, payment plans, Stripe seam |
| `portal` | One branded client hub |
| `automations` | Workflow rules, delayed retention, owner digest |
| `subscriptions` | Flat hosted SaaS billing |
| `pipeline` / `jobs` | Durable background processing |

## Local Development

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
bash scripts/start-hestia.sh
```

Useful URLs:

- `/` landing
- `/demo` public buyer tour for wedding, food & beverage, and real-estate workflows
- `/pricing` flat $40/month value stack and trial conversion page
- `/interest` public beta access form with first-party attribution
- `/signup` hosted signup when `HESTIA_SIGNUP_ENABLED=true`
- `/login` owner login
- `/dashboard` studio command center
- `/settings/account` hosted account, URLs, custom domain, billing actions
- `/settings/billing` flat plan billing page
- `/admin` operator admin with `HESTIA_API_TOKEN`
- `/admin/launch` beta launch kit with founder operating checklist, beta interest leads, cohort summary, tagged invite links, CSV export, cooldown-safe trial nudges, and owner follow-up queue
- `/admin/trials` trial conversion cockpit for stalled and activated studios; tenant detail includes a beta conversion timeline
- `/healthz` liveness
- `/readyz` readiness

## Provider Seams

Everything runs without external keys by default.

| Seam | Env | Default | Real provider |
|------|-----|---------|---------------|
| Vision / Album / Content / Product | `HESTIA_*_BACKEND` | `mock` | `xai` |
| Storage | `HESTIA_STORAGE_BACKEND` | `local` | `s3` |
| Payments | `HESTIA_PAYMENTS_BACKEND` | `mock` | `stripe` |
| Subscriptions | `HESTIA_SUBSCRIPTION_BACKEND` | `mock` | `stripe` |
| Email | `HESTIA_EMAIL_BACKEND` | `mock` | `smtp` |
| Print fulfillment | `HESTIA_FULFILLMENT_BACKEND` | `mock` | `lab` |

Real backends should fail loudly in logs and degrade to safe behavior where the
product path allows it. Billing and payment secrets stay in untracked environment
files.

## Architecture

Hestia is a lightweight hosted SaaS built with:

- FastAPI
- Jinja2 and HTMX
- SQLite with WAL
- Forward-only migrations with a migration ledger
- Tenant-scoped data model
- Structured JSON logs with request IDs
- Durable SQLite-backed job queue with retry and crash reclaim
- Local/S3 storage seam
- Stripe-ready payment and subscription seams

Details: [`docs/architecture.md`](docs/architecture.md)

## Verification

```bash
bash scripts/ci-smoke.sh        # ruff + pytest + healthz boot
bash scripts/dogfood-hestia.sh  # end-to-end magic moment smoke
bash scripts/hosted-preflight.sh --url https://yourdomain.com
```

The test suite covers tenant isolation, hosted routing, public demo and pricing
pages, first-party signup attribution, beta launch kit, flat-plan billing, signup,
onboarding presets, trial conversion analytics, custom domains, proposal accept
flows, offer idempotency, public tokens, payments, client portal flows, and safe
mock-provider operation.

`scripts/hosted-preflight.sh` reads the same `.env`/environment values as the app
and fails on hosted blockers: default secrets, non-HTTPS public URL, missing hosted
domain, wrong $40/month or 14-day trial contract, missing Stripe subscription
secrets, mock email for signup verification, unwritable volumes, missing Docker/Caddy
assets, or failing `/healthz`/`/readyz` probes. Set `HESTIA_PREFLIGHT_URL` or pass
`--url` after the app is running.

## Hosted Launch Checklist

1. Buy or choose the hosted domain.
2. Point apex and wildcard DNS at the host.
3. Set `HESTIA_DOMAIN` and `HESTIA_PUBLIC_URL`.
4. Generate strong `HESTIA_SECRET_KEY`, `HESTIA_API_TOKEN`, and CSRF/session secrets.
5. Configure SMTP for verification and owner emails.
6. Configure Stripe live keys and webhook secret.
7. Confirm Stripe checkout creates the single $40/month subscription with a 14-day trial.
8. Choose local volume or S3/R2 storage and verify backups.
9. Run `docker compose up --build -d`.
10. Run `/healthz`, `/readyz`, `scripts/ci-smoke.sh`, `scripts/dogfood-hestia.sh`,
    and `scripts/hosted-preflight.sh --url https://yourdomain.com`.
11. Create one test studio through `/signup`.
12. Install each onboarding preset once: wedding, food & beverage, real estate.
13. Start and cancel a test subscription.
14. Verify custom-domain pending and admin verification flow.
15. Publish the launch post and invite the first 5-10 studios manually.

## X Launch Thread Outline

**Post 1**

I built Hestia for photographers who are tired of running a studio across 5-7
separate tools.

Everything you need to run a professional studio - booking, CRM, contracts,
galleries, AI offers, invoices, payments, and retention - hosted and maintained
for $40/month.

14-day free trial. No tiers.

**Post 2**

The wedge: Hestia owns the full client-to-cash workflow.

Inquiry -> booking -> proposal -> contract -> deposit -> questionnaire -> gallery
-> AI-curated offer -> payment -> fulfillment -> retention.

One studio command center instead of duct tape.

**Post 3**

The magic moment: upload a gallery and Hestia creates a client-ready offer link
from the gallery's AI signal.

No duplicate links on re-run. No extra sales tool. No manual bundle building.

**Post 4**

The first presets are built for wedding, food & beverage, and real-estate
photographers.

Pick your niche, and Hestia seeds booking types, packages, intake forms, draft site
copy, and sample workflow data.

**Post 5**

Why $40/month?

Because growing studios should not need a booking app, CRM, contract app, gallery
host, invoice app, AI helper, and retention spreadsheet just to look professional.

Hestia is the hosted studio OS at a flat, accessible price.

## Roadmap

The whole studio lifecycle is implemented end to end:

```text
visitor -> inquiry -> booking -> proposal -> contract -> deposit -> questionnaire
  -> shoot -> gallery -> proofing -> AI-curated offer -> sale
  -> fulfillment -> retention
```

Near-term product work:

- Add production-grade custom-domain TLS automation.
- Deepen the live vision backend for culling, duplicate detection, blink detection,
  and studio style profiles.
- Add operator backup/restore runbooks for SQLite volumes and media storage.
- Deepen the public demo into short hosted walkthrough videos once the first
  studios have shipped real workflows.

## License

TBD - under active development by [Kevin Lee](https://github.com/Ayyitskevin).

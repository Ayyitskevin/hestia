# Deployment wiring: Stripe, SMTP, storage

The exact external-service setup for a live Hestia box, distilled from the code so
it's copy-paste, not guesswork. Pairs with `.env.production.example` and
`docs/launch-checklist.md`.

## Stripe

Hestia uses Stripe for **two** money paths, both behind the same API keys:

- **Studio subscriptions** — each studio pays $40/month (`HESTIA_SUBSCRIPTION_BACKEND=stripe`).
- **Client invoice payments** — a studio's clients pay invoices via Stripe Checkout
  (`HESTIA_PAYMENTS_BACKEND=stripe`). Leaving this on `mock` marks invoices paid with
  nothing charged, so preflight **fails** on it.

### Keys

1. Dashboard → Developers → API keys → copy the **live** secret key (`sk_live_…`) into
   `HESTIA_STRIPE_SECRET_KEY`.

### Webhook (required — it's what completes every payment)

2. Dashboard → Developers → Webhooks → **Add endpoint**.
   - URL: `https://YOUR_DOMAIN/webhooks/stripe`
   - Events to send (exactly these — Hestia ignores the rest):
     | Event | What it drives |
     |-------|----------------|
     | `checkout.session.completed` | marks an invoice paid + fulfills its order; activates a studio's subscription |
     | `customer.subscription.updated` | syncs trial → active, and active → past_due (failed card) |
     | `customer.subscription.deleted` | downgrades a canceled studio to the free Beta plan |
3. Copy that endpoint's **Signing secret** (`whsec_…`) into `HESTIA_STRIPE_WEBHOOK_SECRET`.
   Without it the webhook returns `503` and no payment ever completes.

The endpoint verifies the `Stripe-Signature` (HMAC-SHA256, constant-time, replay
window) before acting, is idempotent (a redelivered event never double-settles), and
returns `200` for events about unknown tenants so Stripe won't retry forever.

### Verify

- `bash scripts/hosted-preflight.sh` → `subscription backend`, `stripe secrets`, and
  `invoice payments` all **pass**; `stripe mode` reads **live**.
- Stripe dashboard → send a test `checkout.session.completed` → endpoint returns `200`.
- End-to-end: subscribe a test studio with a real card, confirm it goes `active` and
  the receipt email arrives; then refund/cancel and confirm the downgrade lands.

## SMTP

Signup verification, client email, and operator digests all send over SMTP
(`HESTIA_EMAIL_BACKEND=smtp`). On `mock` they only record to the in-app outbox, so a
new studio can never verify its email and activate.

- `HESTIA_SMTP_HOST`, `HESTIA_SMTP_PORT` (587 STARTTLS by default),
  `HESTIA_SMTP_USER`, `HESTIA_SMTP_PASSWORD`.
- `HESTIA_SMTP_FROM` is the visible `From:` — falls back to `HESTIA_SMTP_USER` if unset.
- Use a sender on your own domain with **SPF + DKIM** published, or verification mail
  lands in spam and studios can't onboard.

Verify: preflight `email backend` + `smtp config` pass; then sign up with a real
personal inbox and confirm the verification link actually arrives.

## Storage

- **local** (default): the `hestia-data` Docker volume. Backed up daily by the compose
  `backup` service; restore drill in `docs/backup-restore.md`. Good for launch.
- **s3** (`HESTIA_STORAGE_BACKEND=s3` + `HESTIA_S3_BUCKET`, plus AWS creds via the
  standard chain): use a **private** bucket so images are served by short-lived
  presigned URLs. Do **not** set `HESTIA_S3_PUBLIC_BASE_URL` to a public/CDN bucket for
  client galleries — that serves enumerable object keys and defeats the per-image
  capability-token privacy the local backend and presigned mode guarantee.

## Reminder

`chmod 600 .env` — it holds live Stripe and SMTP secrets. It's git-ignored; keep it
off every shared drive.

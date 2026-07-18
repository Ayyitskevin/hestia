# Release-candidate wiring: studio billing, SMTP, storage

This describes the current external-service seams; it is not authorization for a live
launch. Pair it with `.env.production.example`, `docs/launch-checklist.md`, and the
D1-D5 holds in [`HUMAN-DECISIONS.md`](HUMAN-DECISIONS.md). Client-invoice payments,
anonymous media authorization, and durability evidence remain held until their approved
contracts are implemented and verified.

## Stripe

Hestia currently contains two money paths behind the same platform API key, but only
studio subscriptions are in the release-candidate operating scope:

- **Studio subscriptions** charge each studio $40/month through
  `HESTIA_SUBSCRIPTION_BACKEND=stripe`.
- **Client invoice payments** currently create platform-account Checkout Sessions
  without connected-account routing or a stored attempt binding. The mock path marks
  invoices paid without charging.

**Release-candidate boundary:** keep `HESTIA_PAYMENTS_BACKEND=mock` so preflight stays
red. Mock checkout is still routable and settles locally, so keep the entire candidate
behind loopback, SSH, or source-allowlisted ingress. D2 targets Stripe Connect direct
charges; there are no live client-invoice verification steps until that implementation
is separately approved and lands.

### Studio subscription keys

1. Use a Stripe **test** secret (`sk_test_…`) in `HESTIA_STRIPE_SECRET_KEY` for the
   private rehearsal. Switch to a live key only after the complete Day-7 gate—not merely
   D1-D5 implementation evidence—is complete.

### Studio subscription webhook

2. In test mode, forward Stripe test events to `/webhooks/stripe` inside the private
   boundary. Do not register the public live-domain endpoint until Day 7.
   - Events needed by the current subscription path:
     | Event | What it drives |
     |-------|----------------|
     | `checkout.session.completed` | activates a studio subscription; the current invoice branch remains held |
     | `customer.subscription.updated` | syncs trial → active, and active → past_due (failed card) |
     | `customer.subscription.deleted` | downgrades a canceled studio to the free Beta plan |
3. Copy the test-forwarder's **Signing secret** (`whsec_…`) into
   `HESTIA_STRIPE_WEBHOOK_SECRET`; without it, subscriptions cannot activate.

The endpoint verifies the `Stripe-Signature` with a bounded replay window before
acting. The same handler still parses invoice metadata and can mark and fulfill a
current platform-charge invoice; that branch has no D2 Connect attempt binding and
remains outside the authorized release-candidate scope. Events for unknown tenants are
acknowledged so Stripe does not retry them forever.

### Verify the subscription path

- `bash scripts/hosted-preflight.sh`: subscription and Stripe-secret checks pass;
  test-key `stripe mode` warns; self-service signup and invoice payments fail.
- Send a subscription-shaped `checkout.session.completed` test event and confirm the
  endpoint returns `200` with the expected studio subscription transition.
- Inside the private boundary, subscribe a controlled tenant with a Stripe test card;
  confirm activation and receipt, then cancel/refund in test mode.
- Restore signup to false and do not create a client invoice. Client-payment
  verification begins only after the approved D2 Connect implementation exists.

## SMTP

Signup verification, client email, and operator digests all send over SMTP
(`HESTIA_EMAIL_BACKEND=smtp`). On `mock` they only record to the in-app outbox, so a
new studio can never verify its email and activate.

- `HESTIA_SMTP_HOST`, `HESTIA_SMTP_PORT` (587 STARTTLS by default),
  `HESTIA_SMTP_USER`, `HESTIA_SMTP_PASSWORD`.
- `HESTIA_SMTP_FROM` is the visible `From:` — falls back to `HESTIA_SMTP_USER` if unset.
- Use a sender on your own domain with **SPF + DKIM** published, or verification mail
  lands in spam and studios can't onboard.

For a private SMTP rehearsal, temporarily enable signup, use a controlled personal
inbox, confirm the verification link arrives, then restore
`HESTIA_SIGNUP_ENABLED=false`.

## Storage

- **local** (default) persists in the `hestia-data` Docker volume. The compose backup
  service makes daily SQLite artifacts; local DB and media on one host are not launch
  durability. D5 must add and verify an off-site DB+required-media copy.
- **s3** (`HESTIA_STORAGE_BACKEND=s3` + `HESTIA_S3_BUCKET`, plus AWS credentials via
  the standard chain) must use a **private** bucket.
  `HESTIA_S3_PUBLIC_BASE_URL` stays blank because public/CDN object URLs expose
  enumerable keys outside gallery visibility and capability-token checks. Current
  browser rendering still emits presigned provider URLs, so D3 same-origin
  authorization/revocation parity must land before this becomes the launch path.
- **D5 evidence, regardless of backend:** use a non-deleting copy for the database and
  every required gallery-media object, with destination-side versioning, object lock,
  or equivalent same-path retention. Verify the newest DB remotely, write a fresh
  non-secret receipt only after both scopes succeed, and recover SQLite plus a known
  gallery's media from a real remote artifact.

## Reminder

`chmod 600 .env` — it holds Stripe and SMTP secrets (test during private rehearsal,
live only after the Day-7 gate). It is git-ignored; keep it off every shared drive.

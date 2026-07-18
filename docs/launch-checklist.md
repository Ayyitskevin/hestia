# 7-day launch checklist

This is a release-candidate rehearsal, not current launch authorization. Work from a
bare box toward beta readiness one focused day at a time. Every gate needs evidence.
Before Day 7, the owner must approve D1-D5 in
[`HUMAN-DECISIONS.md`](HUMAN-DECISIONS.md), and each approved contract must be
implemented and verified. The committed environment keeps self-service signup off,
Stripe in test mode, and `HESTIA_PAYMENTS_BACKEND=mock`. Hosted preflight must fail
`self-serve signup` and `invoice payments` and warn on `stripe mode`.
Mock checkout is not technically disabled: it can still mark paid and fulfill locally.
The environment and this checklist are procedural holds, not a server-side boundary.
The separately approved non-environment release policy must cover preflight/Admin Launch,
invite helpers and redemption, signup, client Checkout, the invoice webhook branch, and
anonymous media before public use.
Keep this candidate unreachable from the public Internet—use loopback, SSH, or a
source-allowlisted private network—until Day 7. Do not clear D2 by selecting today's
platform-charge invoice path.

## Day 1 — box + DNS

- [ ] Provision a small Linux box (2 GB RAM is plenty) with Docker + compose.
- [ ] Prepare, but do not activate, the apex `A` and wildcard `*.HESTIA_DOMAIN`
      records. Tenant sites will eventually resolve as `{slug}.domain`.
- [ ] Keep public ingress on ports 80/443 blocked. Rehearse through loopback, SSH, or a
      source-allowlisted private network. Compose publishes both ports on all interfaces,
      so enforce this at the host firewall/edge.
- [ ] Record the firewall/DNS opening commands for Day 7; do not execute them yet.
- TLS is deferred with public ingress. After the holds close, Caddy obtains the apex
      certificate and issues per-subdomain certificates on demand, gated by
      `/internal/tls-check`.

## Day 2 — configure + boot

- [ ] `cp .env.production.example .env` — it is a held release-candidate template.
      Fill the four `<SET_ME>` groups: domain, secrets
      (`openssl rand -hex 32` for each `CHANGE_ME`), studio-subscription test keys,
      and SMTP. Follow [`deploy-wiring.md`](deploy-wiring.md). Leave self-service
      signup off and `HESTIA_PAYMENTS_BACKEND=mock`; those preflight failures are
      deliberate procedural holds. Mock checkout remains functional, so do not expose
      the candidate beyond the private boundary.
- [ ] `chmod 600 .env`
- [ ] Config gate (no URL yet): expect `self-serve signup` and `invoice payments` to
      fail, and `runtime probe` plus test-key `stripe mode` to warn. Every other check
      must pass or have a documented D1-D5 hold:
      ```sh
      bash scripts/hosted-preflight.sh
      ```
      Save the output; never treat today's platform-charge invoice path as a fix.
- [ ] Before first boot, prove the target data volume is empty. If any retained database
      exists, stop: make a WAL-safe snapshot, run `python -m hestia.migration_audit`
      against the isolated copy, and resolve D4. App boot auto-applies pending migrations;
      booting first would destroy the pre-migration evidence.
- [ ] `docker compose up -d --build` only behind the private ingress boundary.
- [ ] Probe health and readiness from inside the container, not through public DNS:
      ```sh
      docker compose exec hestia python -c \
        'from urllib.request import urlopen; print(urlopen("http://127.0.0.1:8500/healthz").read()); print(urlopen("http://127.0.0.1:8500/readyz").read())'
      ```
- [ ] Do not run hosted preflight with `--url` or activate public DNS/ingress until
      Day 7.
- [ ] Confirm the backup service made its first artifact (preflight's
      `backup freshness` check flips from warn to pass):
      `docker compose ps` shows `backup` up, not restarting.

## Day 3 — money + email reality check

- [ ] Stripe studio subscriptions only: use test keys and a test webhook forwarded
      inside the private boundary; confirm the expected subscription transitions.
- [ ] Do not register a live endpoint or exercise client-invoice Checkout. The current
      platform-account path remains held by D2.
- [ ] For SMTP, temporarily enable signup only inside the private boundary and use a
      controlled personal address. The verification email must arrive in an inbox.
- [ ] Restore `HESTIA_SIGNUP_ENABLED=false` and delete the test tenant afterwards.

## Day 4 — demo studios + tours

- [ ] Admin → Launch → **Seed founder demos**: all four niches (wedding, portrait,
      food, real estate) report Ready with a processed showcase gallery.
- [ ] Walk `/demo/wedding`, `/demo/portrait`, `/demo/food`, `/demo/real-estate` —
      each renders its own tour.
- [ ] Open each demo studio page and showcase gallery through the private boundary:
      the AI cull (blink + duplicate hidden) and album in review are visible. Seeded
      demo booking/deposit paths must remain unreachable from the public Internet.

## Day 5 — test-mode subscription rehearsal

- [ ] Inside the private boundary, temporarily enable signup and complete onboarding;
      confirm the 14-day trial state, then keep the resulting test tenant controlled.
- [ ] Subscribe with a Stripe test card for the $40 test amount. Confirm the test
      subscription activates and its receipt arrives.
- [ ] Cancel/refund in Stripe test mode and confirm the plan downgrade lands in Hestia.
- [ ] Restore `HESTIA_SIGNUP_ENABLED=false`.
- [ ] Do not create or settle a client invoice. D2 requires a separately approved
      Connect implementation and evidence; subscription rehearsal does not cover it.

## Day 6 — backup/restore drill on the private candidate

- [ ] Run the full drill from `docs/backup-restore.md` on a scratch data dir —
      restore an artifact taken from the live volume, verify `integrity_check: ok`.
- [ ] Use the approved D5 provider/path with destination-side versioning, object lock,
      or equivalent same-path retention.
- [ ] Run the non-deleting off-site copy for both DB backups and required gallery media,
      then verify the newest DB artifact remotely.
- [ ] Confirm the fresh, non-secret receipt identifies storage mode, destination,
      newest DB artifact, and completion time.
- [ ] Start a recovery from a real remote artifact and verify SQLite plus one known
      gallery's media. A local-only restore is not the D5 drill.

## Day 7 — public release gate

- [ ] Record explicit owner approvals for D1-D5 and link the implementation and
      verification evidence for every approved contract.
- [ ] Verify the separately approved non-environment release policy fails closed across
      preflight, Admin Launch/share/invite actions, the core invite helpers and redemption,
      signup, client Checkout (including zero-due), the invoice branch of the shared
      Stripe webhook, and anonymous media. No environment/free-text approval bypass exists.
- [ ] Close the separate custom-domain/public-edge review and prove the production SQLite
      runtime includes the WAL-reset fix or an approved vendor backport.
- [ ] Replace test/procedural holds only with those approved implementations: configure
      live studio-subscription Stripe plus the approved D2 client-payment path, then set
      self-service signup to the approved state.
- [ ] Activate the prepared DNS records and open only ports 80/443.
- [ ] Register the live Stripe endpoint with the events required by studio subscriptions
      and the implemented D2 Connect contract.
- [ ] Final `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"` —
      zero fails, zero unexplained warns.
- [ ] Confirm the mock invoice action is unreachable, D2 settlement evidence is green,
      and the D5 remote receipt/recovery evidence is fresh.
- [ ] Send the first beta invite batch from the admin beta cockpit.
- [ ] Watch the launch digest: first signups, trial starts, and nudges are now on
      the worker's clock, not yours.

Doors open. From here the ongoing cadence — daily glance, weekly backup check,
quarterly restore drill, incident quick-reference — lives in
[`operations.md`](operations.md).

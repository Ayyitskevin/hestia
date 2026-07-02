# 7-day launch checklist

The founder go-live runbook: from a bare box to the first beta invites, one focused
day at a time. Every gate is a command you can run — no vibes, no "should be fine."

## Day 1 — box + DNS

- [ ] Provision a small Linux box (2 GB RAM is plenty) with Docker + compose.
- [ ] Point DNS at it: apex `A` record for `HESTIA_DOMAIN` **and** wildcard
      `*.HESTIA_DOMAIN` (tenant sites resolve as `{slug}.domain`).
- [ ] Open ports 80 and 443. Nothing else.

## Day 2 — configure + boot

- [ ] `cp .env.example .env`, then set: `HESTIA_DOMAIN`, `HESTIA_PUBLIC_URL`,
      real secrets (`openssl rand -hex 32` for each `CHANGE_ME`),
      `HESTIA_SIGNUP_ENABLED=true`.
- [ ] `chmod 600 .env`
- [ ] Config gate (no URL yet — runtime probe will warn, everything else must pass):
      ```sh
      bash scripts/hosted-preflight.sh
      ```
- [ ] `docker compose up -d --build`
- [ ] Full gate against the live domain — healthz, readyz, **and** the live
      robots.txt privacy check must pass:
      ```sh
      bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"
      ```
- [ ] Confirm the backup service made its first artifact (preflight's
      `backup freshness` check flips from warn to pass):
      `docker compose ps` shows `backup` up, not restarting.

## Day 3 — money + email reality check

- [ ] Stripe: live keys in `.env`, webhook endpoint added in the Stripe dashboard
      pointing at the live domain, `stripe mode` preflight check reads live.
- [ ] SMTP: sign up with a real personal address end-to-end — the verification
      email must arrive in an inbox, not a log.
- [ ] Delete the test tenant from the admin panel afterwards.

## Day 4 — demo studios + tours

- [ ] Admin → Launch → **Seed founder demos**: all four niches (wedding, portrait,
      food, real estate) report Ready with a processed showcase gallery.
- [ ] Walk `/demo/wedding`, `/demo/portrait`, `/demo/food`, `/demo/real-estate` —
      each renders its own tour.
- [ ] Open each demo studio's public page and its showcase gallery: the AI cull
      (blink + duplicate hidden) and the album in review are visible.

## Day 5 — dry-run the money path

- [ ] Real signup → onboarding preset → 14-day trial starts.
- [ ] Subscribe with a real card ($40). Confirm the subscription goes active and
      the receipt email arrives.
- [ ] Cancel + refund from Stripe, confirm the plan downgrade lands in Hestia.

## Day 6 — backup/restore drill on the live box

- [ ] Run the full drill from `docs/backup-restore.md` on a scratch data dir —
      restore an artifact taken from the live volume, verify `integrity_check: ok`.
- [ ] Wire the off-site copy (rsync/rclone of `backups/`) and confirm one artifact
      actually landed off the box. One machine is zero backups.

## Day 7 — open the doors

- [ ] Final `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"` —
      zero fails, zero unexplained warns.
- [ ] Send the first beta invite batch from the admin beta cockpit.
- [ ] Watch the launch digest: first signups, trial starts, and nudges are now on
      the worker's clock, not yours.

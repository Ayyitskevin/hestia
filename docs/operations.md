# Running Hestia in production

Once the box is live (`docs/launch-checklist.md`), this is the ongoing cadence to
keep it healthy. Hestia is a single container + Caddy + a daily backup sidecar, so
"ops" is light — but light isn't zero. Everything here is a real command or admin
page.

## The one-glance health check

- **`/admin/system`** — version, queue depth, failed/stale jobs, applied migrations,
  backend seams (which are live vs mock), and config warnings. Your daily glance.
- **`/admin/integrity`** — per-tenant data-integrity overview.
- **`GET /healthz`** (liveness) and **`GET /readyz`** (DB + migrations + storage).

## Daily (30 seconds)

- [ ] `docker compose ps` — `hestia`, `caddy`, and `backup` all `Up`. A restarting
      `backup` means backups are failing (see below) — treat it as page-worthy.
- [ ] Glance at `/admin/system`: no growing **failed jobs** pile, no red config
      warnings. A few stale jobs self-reclaim; a climbing failed count doesn't.
- [ ] While onboarding a cohort: `/admin/launch` for the ranked next action and
      `/admin/trials` for the **Past due** count (see `docs/beta-onboarding.md`).

## Weekly

- [ ] Confirm backups are current — `/admin/system` (or `hosted-preflight.sh --url …`
      `backup freshness` check) should never be stale. Artifacts live at
      `/data/backups/hestia-*.db.gz`.
- [ ] Confirm the **off-site** copy actually pulled the newest artifact. One machine
      is zero backups.
- [ ] Skim access logs for anomalies (auth failures, 5xx spikes) — structured JSON,
      one line per request, no client tokens (they're redacted).
- [ ] Requeue any genuinely-stuck failed jobs from `/admin/system` after reading why
      they failed.

## Monthly

- [ ] Dependency + base-image refresh: rebuild (`docker compose build --pull`) to pick
      up `python:3.12-slim` security patches; review `pip` updates. Run
      `bash scripts/ci-smoke.sh` before deploying the rebuilt image.
- [ ] Rotate nothing on a schedule you don't have to — but confirm secrets are still
      the strong values from launch, and `.env` is still `chmod 600`.
- [ ] Review Stripe + SMTP dashboards for silent failures (webhook delivery errors,
      bounced mail).

## Quarterly

- [ ] **Restore drill** — the whole point of backups. Follow `docs/backup-restore.md`
      on a scratch `HESTIA_DATA_DIR` (or staging), restoring a real artifact from the
      live volume; confirm `integrity_check: ok` and a known studio is present. Never
      let production be your first-ever restore.
- [ ] Re-run the full preflight against the live domain and read every line:
      `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"`.
- [ ] Delete stale `pre-restore-*.db` safety copies after a successful drill.

## Deploying a change

1. `bash scripts/ci-smoke.sh` locally (ruff → pytest → boot → privacy invariants).
2. `git pull` on the box, `docker compose up -d --build`.
3. `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"` → zero fails.
4. Migrations apply automatically on boot (forward-only, ledgered); `/readyz` turns
   green when the schema is current.

## Incident quick-reference

| Symptom | First look | Runbook |
|---------|-----------|---------|
| Site down / 502 | `docker compose ps`, `docker compose logs hestia` | restart: `docker compose restart hestia` |
| `backup` container restarting | `docker compose logs backup` (missing DB? bad dir?) | `docs/backup-restore.md` |
| Payments not completing | Stripe dashboard → webhook delivery; `/webhooks/stripe` reachable? | `docs/deploy-wiring.md` |
| Verification/emails not arriving | `/admin/system` email seam; SMTP creds; SPF/DKIM | `docs/deploy-wiring.md` |
| A studio stuck "trialing" after paying | missed `customer.subscription.updated` webhook | `docs/deploy-wiring.md` |
| Need to roll back data | stop app, restore last good artifact | `docs/backup-restore.md` |
| Subdomain has no TLS | first hit issues on-demand (brief delay); check `/internal/tls-check` | `docs/launch-checklist.md` |

## What you deliberately don't have to do

Hestia runs its own follow-ups on the background worker — trial nudges, card-failed
dunning, overdue-invoice and unsigned-document reminders, owner/launch digests,
gallery sale campaigns, recurring invoices — all on shared cooldowns. Don't send these
by hand; the worker won't double up with you.

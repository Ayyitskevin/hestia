# Running Hestia in production

Once the box is live (`docs/launch-checklist.md`), this is the ongoing cadence to
keep it healthy. Hestia is a single container + Caddy + a daily backup sidecar, so
"ops" is light — but light isn't zero. Everything here is a real command or admin
page.

## The one-glance health check

- **`/admin/system`** — version, queue depth, failed/stale jobs, applied migrations,
  backend seams (which are live vs mock), tracked upload metadata, and config warnings.
  Your daily glance.
- **`/admin/integrity`** — per-tenant data-integrity overview.
- **`GET /healthz`** (liveness) and **`GET /readyz`** (DB + migrations + storage).

## Know when it's down before a client does

The checks above only run when *you* look. Point a free external monitor
(UptimeRobot, Better Stack, Pingdom — any of them) at
**`https://$HESTIA_DOMAIN/readyz`** on a 1–5 minute interval with email/push alerts.
`/readyz` is the right probe: it exercises DB, migrations, and storage, so it
catches "up but broken," not just "down." Two minutes of setup buys you the
difference between *you* telling a studio about an outage and a studio telling you.
(Optional: a second monitor on a founder-demo studio's public page verifies the
tenant-serving path end-to-end.)

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
- [ ] Confirm the approved **D5 evidence**, not only that `scripts/offsite-sync.sh`
      exited zero: verify the fresh receipt, the newest version-retained remote DB
      artifact, and required media scope. The current script reports transfer commands
      as unverified and cannot satisfy this check until receipt/remote verification lands.
      One machine is zero backups. See `docs/backup-restore.md`.
- [ ] Skim access logs for anomalies (auth failures, 5xx spikes) — structured JSON,
      one line per request, no client tokens (they're redacted).
- [ ] When an xAI backend is live, filter for logger `hestia.xai` and action
      `xai.request`. Each call records only its operation path, HTTP status, and
      duration; rising failures or latency are actionable without exposing prompts,
      images, model output, exception detail, or API keys.
- [ ] Requeue any genuinely-stuck failed jobs from `/admin/system` after reading why
      they failed.

### Reading the storage footprint

`/settings/account` shows an owner the studio's tracked upload footprint;
`/admin/system` shows the operator total and top studios. The denominator is the exact
known byte metadata for gallery originals and project attachments. An untrusted-row
warning means missing, impossible, or invalid size/storage-key metadata was excluded.
Relationship-inconsistent rows are excluded from attribution and reported separately
under Integrity. Investigate either warning before using the number for planning.

This is not a bucket inventory or provider bill. It does not include thumbnails,
generated product renders, orphaned/missing objects, filesystem/object-store overhead,
versioning/replication, requests, retrieval, transfer, the SQLite DB/WAL, or backups.
No quota or invoice decision is made from this view. Dollar rates, limits, and
packaging remain owner-approved product/financial decisions.

## Monthly

- [ ] Dependency + base-image refresh: audit `requirements/runtime.lock` and
      `requirements/pillow-compat.lock` with
      `python -m pip_audit --vulnerability-service=pypi --strict --require-hashes --disable-pip -r requirements/runtime.lock -r requirements/pillow-compat.lock`,
      review the advisory `requirements/dev.lock` scan, then rebuild
      (`docker compose build --pull`) to pick up `python:3.12-slim` security
      patches. Run `bash scripts/ci-smoke.sh` before deploying the rebuilt image.
      Security posture reference: `docs/security.md`.
- [ ] Rotate nothing on a schedule you don't have to — but confirm secrets are still
      the strong values from launch, and `.env` is still `chmod 600`.
- [ ] Review Stripe + SMTP dashboards for silent failures (webhook delivery errors,
      bounced mail).

## Quarterly

- [ ] **Restore drill** — the whole point of backups. Follow `docs/backup-restore.md`
      on a scratch `HESTIA_DATA_DIR` (or staging), starting from a real versioned
      artifact downloaded from the approved remote. Recover required media from that
      remote too; confirm `integrity_check: ok` and one known gallery's bytes/rendering
      via `python -m hestia.recovery verify … --require-media`. Record `rpo_seconds` and
      wall-clock RTO. A live-volume artifact is not the quarterly D5 source.
- [ ] Run `python -m hestia.migration_audit` against the restored scratch DB. Read every
      finding; exit 1 needs the D4 owner decision, and exits 2–3 are holds. The command
      must never target the live WAL path. See `docs/backup-restore.md`.
- [ ] Re-run the full preflight against the live domain and read every line:
      `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"`.
- [ ] Review `pre-restore-*.db` safety copies under the owner-approved retention policy;
      do not delete recovery evidence automatically.
- [ ] Confirm CI still runs `bash scripts/restore-drill.sh` green on `main` (production-path
      refusal + media consistency + safety copy).

## Deploying a change

1. `bash scripts/ci-smoke.sh` locally (ruff → pytest → boot → privacy invariants).
2. When packaged migration SQL, its manifest, or the runner changes, audit a restored
   real-backup snapshot and stop on any unapproved state; never inspect the live path.
3. `git pull` on the box, `docker compose up -d --build`.
4. `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"` → zero fails.
5. Migrations apply automatically on boot (forward-only, ledgered); `/readyz` turns
   green when the schema is current.

### Rollback after a failed deploy

See the full procedure in `docs/backup-restore.md` (**Rollback after a failed deploy**).

Short form:

1. **Code only** — redeploy the previous known-good image/commit; re-run preflight.
2. **Data wrong** — `docker compose stop hestia`, restore a known-good
   `hestia-*.db.gz` or `pre-restore-*.db` with `--allow-production`, pull matching
   media, run `python -m hestia.recovery verify`, then start.
3. Never hand-edit `schema_migrations` to "undo" a migration; restore instead.

`restore.sh` refuses production-like data dirs (`./data`, `/data`, `/srv/hestia/data`,
…) unless `--allow-production` or `HESTIA_ALLOW_PRODUCTION_RESTORE=1` is set
deliberately.

## Incident quick-reference

User-facing questions ("can't log in", "client lost the link", refunds, exports)
have ready-to-send answers in `docs/support.md` — this table is for the box itself.

| Symptom | First look | Runbook |
|---------|-----------|---------|
| Migration audit is decision-required/inconsistent | restored backup copy; audit JSON findings | `docs/backup-restore.md`, D4 in `docs/HUMAN-DECISIONS.md` |
| Site down / 502 | `docker compose ps`, `docker compose logs hestia` | restart: `docker compose restart hestia` |
| `backup` container restarting | `docker compose logs backup` (missing DB? bad dir?) | `docs/backup-restore.md` |
| Payments not completing | Stripe dashboard → webhook delivery; `/webhooks/stripe` reachable? | `docs/deploy-wiring.md` |
| Verification/emails not arriving | `/admin/system` email seam; SMTP creds; SPF/DKIM | `docs/deploy-wiring.md` |
| A studio stuck "trialing" after paying | missed `customer.subscription.updated` webhook | `docs/deploy-wiring.md` |
| Need to roll back data | stop app, restore last good artifact + verify | `docs/backup-restore.md` |
| Restore/verify fails (missing blobs, bad integrity) | correlation_id in stderr; off-site media pull | `docs/backup-restore.md` |
| Accidental restore toward `./data` refused | expected — use scratch path or `--allow-production` | `docs/backup-restore.md` |
| Subdomain has no TLS | first hit issues on-demand (brief delay); check `/internal/tls-check` | `docs/launch-checklist.md` |
| Failed deploy / bad release | previous image + optional data restore | `docs/backup-restore.md` rollback section |

### Side effects under retry (why requeue is usually safe)

Background work is **at-least-once**. After a crash, stale `running` jobs are reclaimed
and handlers may run again. Money and lifecycle transitions are claim-before-act:

- invoice settle (`mark_paid` / Stripe webhook) — status guard; second delivery is a no-op
- gallery publish — only `draft → published` wins; automations fire once
- appointment confirm/book — only `proposed → confirmed` wins; one notify pair
- SMTP down — outbox records `error:…`; no silent success

Requeue dead-letter jobs from `/admin/system` only after fixing the underlying cause.
Duplicate Stripe events are expected and safe.

### Reading recovery diagnostics

Grep logs for `hestia.recovery` or `correlation_id=`. Fields are privacy-safe (counts,
statuses, paths, timings) — no client tokens or secrets. Full field list:
`docs/backup-restore.md` → **Interpreting diagnostics**.

## What you deliberately don't have to do

Hestia runs its own follow-ups on the background worker — trial nudges, card-failed
dunning, overdue-invoice and unsigned-document reminders, owner/launch digests,
gallery sale campaigns, recurring invoices — all on shared cooldowns. Don't send these
by hand; inspect the queue/outbox before intervening so a manual message does not overlap
scheduled work.

Appointment rescheduling is narrower and explicit: Hestia retains the old queued
confirmation/reminder rows as `done` + superseded, then queues one pair for the new
time. A `done` notification means its handler returned or intentionally skipped; it is
not proof that SMTP delivered the message. The queue is at-least-once across a worker
crash, so inspect the email outbox status when delivery itself matters.

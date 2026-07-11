# Launch week 1 — after the four product slices

Runbook for going from a green `main` to the first paying beta studios. Pairs with
[`launch-checklist.md`](launch-checklist.md) (infra) and [`beta-onboarding.md`](beta-onboarding.md)
(cohort ops). Copy pack: [`beta-emails.md`](beta-emails.md).

## Day 1 — Box + DNS

```bash
cp .env.production.example .env
# Fill: HESTIA_DOMAIN, HESTIA_PUBLIC_URL, secrets, Stripe live, SMTP, HESTIA_XAI_API_KEY
chmod 600 .env
bash scripts/hosted-preflight.sh
docker compose up -d --build
bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"
```

**DNS:** apex `A` + wildcard `*.HESTIA_DOMAIN` → your host. Ports 80/443 only.

**Production AI (recommended):**

```bash
HESTIA_VISION_BACKEND=xai
HESTIA_ALBUM_BACKEND=xai
HESTIA_CONTENT_BACKEND=xai
HESTIA_XAI_API_KEY=<your key>
HESTIA_AI_SUBSIDY_ENABLED=true
HESTIA_AI_SUBSIDY_GALLERIES=1
HESTIA_AI_SUBSIDY_IMAGE_CAP=150
```

## Day 2 — Money + email

| Step | Where |
|------|-------|
| Stripe webhook | Dashboard → `https://$HESTIA_DOMAIN/webhooks/stripe` with `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted` |
| SMTP test | Sign up a test studio → verification email in real inbox |
| Cleanup | Admin → Studios → delete test tenant |

See [`deploy-wiring.md`](deploy-wiring.md) for exact Stripe/SMTP fields.

## Day 3 — Backup drill

Run [`backup-restore.md`](backup-restore.md) on a scratch dir. Wire off-site copy of `backups/`.

## Day 4 — Golden demos (≈32 xAI credits)

1. Sign in at `/admin` with `HESTIA_API_TOKEN`
2. **Admin → Launch → Seed founder demo studios** → confirm **4 / 4 ready**
3. Walk `/demo/wedding`, `/demo/portrait`, `/demo/food`, `/demo/real-estate`
4. Spot-check one showcase gallery: blink + duplicate culled, album in review

```bash
bash scripts/dogfood-hestia.sh
```

## Day 5 — Offers + signup + share

| Step | Action |
|------|--------|
| Your prices | Log into a demo studio → **Settings → Offers** → set print/album prices |
| Open signup | `HESTIA_SIGNUP_ENABLED=true` in `.env` → `docker compose restart hestia` |
| Share | `/pricing`, `/beta`, demo tours from **Admin → Launch → Share and inspect** |

## Day 6–7 — First beta cohort

1. **Admin → Launch → Invite next batch** (5 leads)
2. Personal outreach from [`beta-emails.md`](beta-emails.md) §1
3. Daily skim: **Admin → Launch** ranked checklist + **AI credit ledger** on same page
4. Watch **Admin → Trials** for activation leaks
5. Day ~2 / day ~7 personal emails (§3–4 in beta-emails) — not automated

## AI credit discipline

| What | Credits (approx) |
|------|------------------|
| 4 founder demos (8 img each) | ~32 |
| 5 beta first galleries (≤150 img, subsidized) | ~50–750 (cap at 150 each) |
| Reserve | ~10–15 |

**Subsidy rules (built-in):** each studio gets **one** live-vision gallery process up to **150 images**. Re-process on that gallery stays live. Second gallery uses mock cull until they bring their own key (future).

**Monitor:** Admin → Launch → AI credit ledger, or Admin → System for full breakdown.

## Gate before widening invites

- [ ] `hosted-preflight.sh --url` → zero fails
- [ ] 4/4 founder demos ready with live cull visible
- [ ] One test subscribe + cancel on Stripe
- [ ] First beta signup → verify → onboarding → first gallery → offer link
- [ ] AI ledger shows expected call counts (not runaway)

## Week 2+

See the original game plan: vision torture tests on your own photos, widen invites when funnel holds, day-7 retro template in `beta-onboarding.md` §8.

# Beta onboarding runbook

Your first-cohort playbook once the box is live and green (see
`docs/launch-checklist.md`). Everything here is a button in the operator admin — no
scripts, no SQL. Sign in at `/admin` with your `HESTIA_API_TOKEN`.

## 0. Before you invite anyone

- [ ] `bash scripts/hosted-preflight.sh --url "https://$HESTIA_DOMAIN"` → zero fails.
- [ ] You can reach `/admin` and sign in with the master token.
- [ ] A real signup → verification email → onboarding worked once (your own test
      studio). Delete that test tenant afterward from **Admin → Studios**.

## 1. Seed the founder demo studios

The demos are your sales floor: four fully-worked studios (wedding, portrait, food,
real estate), each with a processed showcase gallery (AI cull applied, delivery on,
album in review) so a prospect sees the moat, not an empty shell.

- **Admin → Launch** (`/admin/launch`) → **Seed founder demo studios**.
- Confirm the panel reads **4 / 4 sample studios ready**.
- Spot-check one: open its **Public** link, then its showcase gallery — the blink and
  the duplicate should be culled, and an album should be shared for review.
- Idempotent: clicking again never duplicates. Safe to re-run.

## 2. Line up the interest list

- **Admin → Launch → Beta interest** shows everyone who requested access via `/beta`
  or `/interest`, with source attribution and status (new / invited / converted).
- Anyone you want in the first cohort but who isn't on the list: have them submit
  `/beta`, or add them however you collect leads — invites only go to interest rows.

## 3. Send the first invites

Two ways, same audit-logged, never-double-send machinery:

- **One at a time:** each lead row has an **Invite** button — good for hand-picking.
- **A whole cohort at once:** **Invite next batch** (set a count, default 5). It
  invites the *oldest waiting* leads first and **never re-sends** to already-invited or
  converted contacts, so it's safe to click repeatedly as the list grows.

Each invite emails a private, single-use `/invite/{token}` link that expires in 7 days
and spins up the studio when redeemed. Start small (5–10) so you can watch what happens
before widening.

## 4. Watch the funnel (the part that used to be a spreadsheet)

- **Admin → Launch** — the revenue pipeline (interest → invited → studio → verified →
  preset → trial → paid), cohort pulse, and a ranked operating checklist telling you
  the single highest-leverage next move. **Export CSV** for your own tracking.
- **Admin → Trials** (`/admin/trials`) — every studio's trial state, activation %,
  churn risk, and next action, plus a **Past due** count if any card has failed.
- **Email digest** (button on the Launch page, also weekly automatically) — pipeline,
  stalled studios, open interest, and paid MRR in one email to you.

## 5. What runs on its own (don't do these by hand)

The worker handles the follow-ups on shared cooldowns, so you never double-email a
studio or client:

- **Trial-ending nudges** — studios with ≤3 days left get a personalized nudge.
- **Card-failed dunning** — a `past_due` studio gets a polite fix-your-card email
  (every 4 days until fixed); it keeps full access meanwhile (grace period).
- **Owner digests, reconnect asks, unsigned-doc and stalled-proposal reminders.**

If you *manually* nudge a studio from the cockpit, the automatic sweep honors the same
cooldown — the two can't collide.

## 6. First-week rhythm

- [ ] Day 1: seed demos, invite the first 5–10, confirm the first signup/verify lands.
- [ ] Daily: skim **Admin → Launch** for the ranked next action; invite the next batch.
- [ ] Watch **Past due** on **Admin → Trials** — a nonzero count means a real card
      failed (dunning is already emailing them; reach out personally if it's a studio
      you know).
- [ ] Let the weekly digest be your Monday standup.

## 7. The first-14-days email arc

The spine is automated — never send these by hand (the cooldowns don't know about
your outbox):

- **Day 0 — welcome**, sent the moment a studio verifies its email. Names the three
  first moves: preset → publish → first gallery.
- **Trial ending**, when ≤3 days remain (worker, cooldown-safe, personalized).
- **Card-failed dunning** after conversion (every 4 days until the card is fixed).

Two personal touches from you close the gap in between. Send them from your own
address, 2–3 sentences, no template smell — personalize the [bracketed] bits:

**Day ~2, if the studio has no gallery yet** (check Admin → Trials → activation):

> Subject: your first gallery
>
> Hey [name] — saw you set up [studio] on Hestia. The moment it clicks for most
> photographers: upload any recent shoot to a gallery and watch the cull and the
> offer draft appear. Takes about two minutes. Anything in your way?

**Day ~7, if activated but no money link yet:**

> Subject: getting paid through [studio]
>
> Hey [name] — your galleries look great. The next ten-minute win: send yourself an
> invoice (Invoices → New) and open the payment link to see exactly what your client
> sees. After that, real client money is one click away. Want a hand setting up
> deposits or payment plans?

Why these stay manual: at first-cohort size, a founder reply-thread converts better
than any automation — and what you learn writing them becomes the next automated
nudge.

## 8. Day-7 and day-30 retro (copy this, fill it in)

Thirty minutes each, calendar them now. Every number comes from **Admin → Launch**
(+ its CSV export) and **Admin → Trials** — no spreadsheet archaeology.

```text
HESTIA BETA RETRO — day [7|30] — [date]

FUNNEL (Admin → Launch)
  interest → invited:        [n] → [n]
  invited → studio created:  [n]   (invite links redeemed)
  studio → verified:         [n]
  verified → preset applied: [n]
  preset → activated:        [n]   (first gallery uploaded)
  activated → money link:    [n]   (first invoice/offer sent)
  paid conversions:          [n]   → MRR $[n]

HEALTH (Admin → Trials + /admin/system)
  past-due studios: [n]      failed jobs: [n]      support emails: [n]
  top 2 support themes: [theme] / [theme]

THE THREE QUESTIONS
  1. Where is the funnel's sharpest drop, and what did the studios
     stuck there say (or do) before stalling?
  2. What did I do by hand twice that the product should do once?
  3. What did activated studios touch first — and does onboarding
     lead with that?

DECISIONS (pick, don't ponder)
  [ ] Widen invites (next batch: [n])   — funnel holds at current size
  [ ] Hold size, fix activation        — verified→activated is the leak
  [ ] Personal-call the stalled cohort — n small enough to just ask
  [ ] Pricing/packaging note: [only if 2+ studios said the same thing]

ONE SENTENCE: what does Hestia know today that it didn't at the last retro?
```

Day-7 leans on questions 1 and 3 (is onboarding landing?); day-30 adds retention:
did week-1 studios come back in week 4, and did anyone pay twice?

## Troubleshooting

- **Invite email didn't arrive** → SMTP. Check `/admin` system health and
  `docs/deploy-wiring.md`; verify SPF/DKIM. Meanwhile the invite is still valid — you
  can copy the link from the lead row.
- **A studio is stuck "trialing" after paying** → that means a Stripe
  `customer.subscription.updated` webhook didn't land. Re-check the webhook endpoint +
  events in `docs/deploy-wiring.md`; Stripe's dashboard shows delivery failures.
- **Backups look stale** (preflight `backup freshness` fails) → the compose `backup`
  service is down; `docker compose ps` will show it restarting. See
  `docs/backup-restore.md`.

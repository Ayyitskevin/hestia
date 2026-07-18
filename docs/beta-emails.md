# Beta email pack

> **HOLD — DRAFT ONLY.** Do not send, publish, or run invite actions from this file until
> the complete Day-7 public release gate passes. D1 AI packaging and D2 client-payment
> semantics are not approved; later copy is positioning material, not a shipped contract.

Copy-paste templates for the first Hestia beta cohort. Pair with
[`docs/beta-onboarding.md`](beta-onboarding.md) for admin UI steps and funnel tracking.

**Rules:**
- Send beta **invites** through **Admin → Launch** (audit-logged, never double-sent).
- Send **day-2** and **day-7** personal notes from your own address — they convert better.
- Never hand-send trial-ending or dunning emails; the worker owns those on cooldown.

---

## 1. Beta invite emails (Admin → Launch → Invite)

Hestia sends these automatically when you click **Invite** or **Invite next batch**.
Customize the SMTP template later at `/settings/messages` if needed. Below are
reference bodies for your own outreach *before* the system invite lands.

### Wedding photographer

**Subject:** Your Hestia beta invite — one studio OS, $40/mo

> Hey [name],
>
> You asked about Hestia on [/beta|Instagram|a friend referral]. I built it for
> wedding photographers who are tired of HoneyBook + Pixieset + a separate invoice app.
>
> One login runs inquiry → contract → gallery → AI-curated print offer → payment.
> I'm inviting a small first cohort — your private link is coming in a separate email
> (7-day window). When you're in, pick the **wedding preset** and upload any recent
> gallery to see the magic moment: cull + offer link in under a minute.
>
> — Kevin

### Portrait & family

**Subject:** Hestia beta — mini-sessions to print sales in one place

> Hey [name],
>
> Portrait studios usually juggle booking, proofing, and print sales across three apps.
> Hestia is the hosted studio OS at **$40/month** — booking drops, AI-culled galleries,
> client favorites that become a print package, and invoicing in one stack.
>
> Your invite link arrives separately. Start with the **portrait preset**, publish your
> site, and run one mini-session gallery through **Process** to see the offer link.
>
> — Kevin

### Food & beverage

**Subject:** Hestia beta for menu launches and repeat clients

> Hey [name],
>
> F&B photographers need fast turnaround, licensing-friendly intake, and retainers —
> not another CRM duct-taped to a gallery host. Hestia runs the full client-to-cash loop
> for **$40/month** flat.
>
> Invite link coming in a follow-up email. Use the **food & beverage preset** and walk
> through a menu-launch gallery: upload → process → share the client offer URL.
>
> — Kevin

### Real estate

**Subject:** Hestia beta — book, deliver, invoice, rebook

> Hey [name],
>
> RE shoots are volume + speed. Hestia handles booking, delivery links, invoice
> collection, and broker rebooking reminders without a separate scheduler and invoicing app.
>
> Your private invite is on its way. Choose the **real estate preset**, book a test
> appointment, and deliver one gallery with the built-in offer + pay flow.
>
> — Kevin

---

## 2. Day-0 welcome (automated — do not send by hand)

Triggered when a studio verifies email. If you need to preview wording, check
`/settings/messages` or the outbox at `/settings/outbox`.

**Spine:** preset → publish site → first gallery.

---

## 3. Day ~2 personal — no gallery yet

Send from your address when **Admin → Trials** shows verified but no activation.

**Subject:** your first gallery

> Hey [name] — saw you set up [studio] on Hestia. The moment it clicks for most
> photographers: upload any recent shoot to a gallery and hit **Process — vision → offer**.
> The cull runs, bundles appear, and you get a client-ready link. Takes about two minutes.
> Anything in your way?

**Variant (wedding):**

> Hey [name] — for wedding studios the ah-ha is usually the same: one gallery upload,
> then share the offer link with a second shooter or planner and ask "would you buy from this?"
> Happy to jump on a 10-minute screen share if useful.

---

## 4. Day ~7 personal — private test-mode rehearsal only

Do not send this to a beta studio while D2 is held. It is a founder-only test-mode
rehearsal after an explicit test policy is available.

**Subject:** getting paid through [studio]

> Private test only: create an invoice and inspect `/pay/…` without real funds. This
> proves the UI, not settlement. Do not send a client link until the approved D2 Connect
> path and its webhook/idempotency evidence are live.

**Variant (portrait / print-heavy):**

> Hey [name] — you already have the offer link. Next step: have a friend heart a few
> favorites on the proofing gallery and refresh the offer page — Hestia auto-builds a
> favorites print package. That's the proofing→sales bridge no gallery host does natively.

---

## 5. Trial-ending nudge (automated — do not send by hand)

Worker sends when ≤3 days remain. Personalized with studio name. If you want to
add a personal line, email *after* the automated nudge (respect cooldown — check
**Admin → Trials** before manual nudge).

**Optional personal add-on (day 12):**

**Subject:** before your trial ends

> Hey [name] — your Hestia trial wraps in a couple days. If Hestia saved you even one
> hour of admin or helped you send one offer link you're proud of, **Billing → Subscribe**
> keeps everything running for $40/mo. If something blocked you, reply — I read every note
> in the first cohort.

---

## 6. X launch thread (5 posts — draft, do not publish)

### Post 1

I built Hestia for photographers who are tired of running a studio across 5–7 separate tools.

Booking, CRM, contracts, galleries, AI offers, invoices, payments, and retention —
hosted and maintained for **$40/month**. 14-day trial. No tiers.

### Post 2

The wedge: Hestia owns the full client-to-cash workflow.

Inquiry → booking → proposal → contract → deposit → gallery → AI-curated offer →
payment → fulfillment → retention. One studio command center instead of duct tape.

### Post 3

The magic moment: upload a gallery and Hestia creates a client-ready offer link from
the gallery's AI signal. Re-process all you want — the link never duplicates.

### Post 4

First presets: wedding, portrait & family, food & beverage, real estate. Pick your niche;
Hestia seeds booking types, packages, intake forms, and sample workflow data.

### Post 5

Why $40/month? Because growing studios shouldn't need a booking app, CRM, contract app,
gallery host, invoice app, AI helper, and retention spreadsheet just to look professional.

---

## 7. Objection handling (DM / call cheat sheet)

| Objection | Response |
|-----------|----------|
| "I already use HoneyBook + Pixieset" | Hestia replaces both *and* wires gallery vision into print offers — Pixieset can't auto-curate bundles from your cull. Flat $40 vs. stacked subscriptions. |
| "Aftershoot does AI culling" | Aftershoot is attacking from the edit side inward. Hestia owns inquiry → contract → invoice → offer as one idempotent system. Different buyer moment. |
| "I don't trust AI culling" | Mock runs deterministic in CI; live xAI is optional. You approve every cull; blink/duplicate flags are advisory. Re-process is idempotent. |
| "$40 is too cheap to be real" | Draft positioning only. Do not promise AI scope, print commissions, or future pricing until D1/D2 and the public offer are approved. |
| "What about my print lab?" | Current fulfillment is mock-first and client-payment settlement is held by D2. Do not claim live Stripe payout or lab fulfillment yet. |

---

## 8. Battlecard one-liners

- **vs HoneyBook:** CRM + contracts + galleries + AI offers — HoneyBook stops at weak delivery.
- **vs Pixieset / Pic-Time:** They sell prints; Hestia sells prints *from your AI cull and client favorites* on the same spine as contracts and invoices.
- **vs Aftershoot:** They edit and deliver; Hestia runs the business OS they don't touch.
- **vs Dubsado:** Dubsado is deep workflow; Hestia is photographer-native delivery + revenue automation at half the stack complexity.

---

## 9. After you ship this pack

Do not use the superseded `launch-week1.md` plan. Resolve D1-D5 and every separate
Day-7 gate, then follow [`launch-checklist.md`](launch-checklist.md). Before Day 7,
private preparation only:

1. Keep ingress private, signup off, Stripe in test mode, and client invoices held.
2. Seed and inspect demos only through the private boundary.
3. Set your prices: **Settings → Offers** (`/settings/offers`).
4. Watch usage: **Admin → System → AI usage ledger**.
5. Do not share copy or invite a cohort until the current checklist's Day-7 gate passes.

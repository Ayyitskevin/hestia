# Hestia Competitive Strategy & Build Roadmap

Originally derived from a verified competitive deep-research pass (2026-06; 24
adversarially verified claims across HoneyBook, Dubsado, Pixieset, Pic-Time,
Aftershoot, and Bain pricing data). Pricing, gallery capability, and Hestia build
status were re-verified against official sources on 2026-07-17. Pairs with
[`HESTIA-DOCTRINE.md`](HESTIA-DOCTRINE.md) (the *what/why*); this doc is the
*competitive landscape + what-to-build-next*.

## Executive summary

The market is collapsing into one fight. CRM incumbents (**Dubsado**, **HoneyBook**)
own booking → contract → invoice, while gallery players (**Pixieset**, **Pic-Time**)
weaponize delivery into automated print-revenue engines. HoneyBook can no longer be
described as gallery-weak: its July 2026 photographer release added native galleries
and mini sessions, with 200 GB on Starter and unlimited gallery storage above it.
The acute threat — **Aftershoot** — now combines AI culling/editing with galleries,
proofing, face search, and print sales, collapsing edit → deliver → sell into one app.

**Hestia's moat is the only thing none of them have: the *whole* loop as one
idempotent system** — inquiry → contract → invoice → retention *and* gallery → AI →
offer — where the same vision signal that culls also auto-builds the offer, drafts
the album, and seeds the marketing pack. Aftershoot attacks from the AI-tool side
inward; Hestia owns the business-OS side it doesn't touch.

**The credibility hole has moved:** Hestia's booking-side table stakes are built on
`main`, and blink scoring, perceptual duplicate clustering, cull application, hero
selection, style profiles, bounded live-provider results, and explicit whole-gallery
fallback and a studio-reviewable calibration snapshot already exist. The remaining
vision gap is evidence: historical model/prompt/style-at-run provenance and an actually
labeled live-quality benchmark. Two product-truth gaps matter
just as much: the print-lab module is still a provider seam without shipping/selected
print semantics, and the flat $40 promise does not yet explain the one-gallery hosted
AI subsidy limit.

## The prioritized build roadmap

Each item is a vertical slice through the tenant → client → project → gallery spine.

> **Build status:** the application foundations for items 1–8, 11, and 12 exist on
> `main`. This records code presence, not production-depth parity. Item 9 has a
> structured mock-first/configurable HTTP lab seam, but not a lab-specific adapter or
> selected-image/options/shipping capture. Item 10 is partially delivered; live-provider
> resilience and a safe offline-labeling snapshot are built, while historical provenance
> and labeled live-quality evidence remain open.

### Phase 1 — Contract-to-cash credibility (makes Hestia a real studio CRM)
1. ✅ **Contracts + e-signature** on the client/project spine (`crm`) — biggest table-stakes gap.
2. ✅ **Payment plans / deposits + milestones** on invoices (`invoices`/`payments`).
3. ✅ **Client portal** — one tenant-scoped URL aggregating contract + invoice + gallery + questionnaire.
4. ✅ **Questionnaires / intake forms** wired public inquiry → CRM lead (`studio` → `crm`).

### Phase 2 — Scheduling + automations (kill the busywork)
5. ✅ **Scheduler** with client self-booking + schedule-bound confirm/reminder emails;
   reschedules supersede the queued pair and same-time retries are side-effect-free.
6. ✅ **Workflow engine** — event-triggered (contract signed, payment paid, delivered, booked,
   appointment confirmed), with scheduled delays (the retention loop is the same engine + a delay).

### Phase 3 — Defend the after-the-shoot loop (vs Aftershoot / Pic-Time)
7. ✅ **Gallery proofing** + client favorites/comments (favorites feed `sales` curation).
8. ✅ **Sales automation campaigns** on `sales` — urgency-gated, time-limited sales **auto-curated
   from Hestia's own vision signal + the client's favorites** (the differentiator Pic-Time cannot match).
9. 🟡 **Print-store fulfillment foundation** (WHCC/Bay Photo class) — purchasable
   offers create a structured lab-order payload through a mock-first/configurable
   generic HTTP seam. A lab-specific adapter plus selected-image, option, shipping
   capture, and production retry semantics remain human-gated.

### Phase 4 — AI compounding + retention (the durable moat)
10. 🟡 **Deepen `vision`** — blink scoring, perceptual duplicate clustering, cull
    application, hero selection, style profiles, bounded live-result validation, and
    explicit whole-gallery fallback exist. A studio CSV now provides one current review
    row per frame with blank labels. Next: historical run provenance and a labeled/live
    quality benchmark. Paid API calls and real/customer photography remain human-gated.
11. ✅ **Retention/upsell automations** (anniversary re-book, review requests, welcome) — delayed rules.
12. ✅ **Mobile-responsive** client + photographer surfaces.

## 90-day execution plan — 2026-07-18

The repository is already broad enough. The next quarter should deepen the transitions
that make the breadth feel like one product, not add another module. The exact planning
base is `804523fc469f8e2f2a8b5ed685273aa97baf47f8`.

The competitive bar moved again: HoneyBook now ships native galleries and mini sessions,
while also pushing two-way texting and Tap to Pay; Pixieset and Pic-Time remain much deeper
in gallery presentation, storage, store, and automatic fulfillment; Aftershoot now joins
culling, retouching, proofing, galleries, and print sales. Hestia therefore cannot win on
feature-count parity. It must make the client-to-cash loop unusually continuous and turn
its shared vision/favorites signal into measurable post-shoot revenue.

### Release lane — held until owner decisions

Keep public ingress, live client payments, and public media closed until D1-D5 and the
separate committed runtime-release boundary are approved, implemented, and verified.
The same security review should close raw capability paths in exception logs. Custom-domain
edge activation, SQLite WAL-reset patch evidence, subscription event ordering, and
license/release truth remain independent gates; a green deterministic CI run does not
substitute for them.

### Days 0–30 — one client home and complete booking state

1. Continue the autonomous continuity queue: gallery/project association repair plus
   bounded dashboard gallery and booking-availability reads are landed; next replace
   dashboard client/project list hydration with direct tenant-scoped counts.
2. Make the client action room the canonical destination. Once its token contract is
   approved, create and send it intentionally, return clients there after sign/pay/form/
   gallery/album actions, and add proposal creation to the project hub.
3. Stop fragmenting repeat clients. Design normalized-email matching plus an explicit
   duplicate-review/merge tool; do not silently merge records from public input.
4. After the money contract is approved, enforce `package total = deposit + scheduled
   balance(s)` for booking, proposal, and mini-session paths.

### Days 31–60 — a delivery experience clients remember

1. Surface the proofing URL immediately after publication and make its send/automation
   context explicit. Do not widen anonymous media authority before D3 is closed.
2. Build a gallery lightbox with large-image navigation, favorites and notes in context,
   mobile gestures, branded cover/story controls, and a clear proofing-to-final-download
   transition.
3. Add client-selectable proposal collections/add-ons and typed questionnaire fields
   before pursuing generalized document-builder depth.
4. Define timezone ownership and external-busy synchronization. Implement it after the
   schema/integration design is approved, because outbound floating-time ICS is not
   protection against real Google/Outlook conflicts.

### Days 61–90 — selection-to-sale pilot

Pilot one narrow, real print path: AI heroes plus client favorites become an editable
photographer-approved recommendation; the client selects frames/options, reviews crop,
provides shipping, pays through the approved connected-account path, and one chosen lab
fulfills with observable retry/reconciliation. Measure gallery-to-offer, offer-to-cart,
cart-to-paid, order margin, fulfillment failure, and time-to-resolution. This is the
defensible wedge. General content publishing, broad marketplace product variants, and a
large lab catalog wait until the pilot earns revenue.

### Parity gaps to track without losing the wedge

| Surface | Current depth | Next proof of credibility |
|---|---|---|
| Client home | Ranked action room exists but enable/send and return context are fragmented | One intentional portal lifecycle and uninterrupted booking checklist |
| Booking economics | Deposits exist; remaining package balance is not consistently created | Total/deposit/balance invariant with approved settlement semantics |
| Gallery delivery | Upload, publish, proofing, favorites, comments, cull, offer, and download exist | Owner-visible proof link plus lightbox-quality client presentation |
| Communication/calendar | Outbound email and calendar subscription exist | Approved two-way messaging and timezone-aware external-busy sync |
| Print commerce | Stable offers, orders, and a generic lab seam exist | Selected frames/options/shipping, one real lab, and reconciliation |
| AI moat | Vision and favorites already curate offers | Historical provenance, labeled benchmark, editable recommendation, measured sales lift |

## Where AI is a wedge vs a gimmick

- **Genuine & defensible:** AI wired *into the revenue loop* — auto-curating sellable
  packages from the gallery's vision signal, album drafts (model proposes, code
  validates), marketing copy at the friction point. Cross-module compounding no point
  tool spans; **this is the moat.**
- **Commodity / gimmick:** standalone "AI editing styles" as a checkbox; any AI that
  doesn't cut admin or drive a sale. High-volume culling is now a table-stakes *floor*
  (Aftershoot owns it) — Hestia needs parity, not a pitch.

## Pricing & packaging

Official monthly prices re-verified 2026-07-17 (annual commitments shown as their
advertised monthly equivalent):

| Product | Entry | More comparable integrated tier |
|---|---:|---:|
| [HoneyBook](https://help.honeybook.com/en/articles/2418282-what-s-included-in-each-honeybook-membership-plan) | Starter $36 monthly / $29 annual | Essentials $59 / $49 |
| [Pixieset Suite](https://pixieset.com/pricing-suite/) | Starter $35 monthly / $28 annual | Pro $50 / $38 |
| [Pic-Time](https://www.pic-time.com/pricing/client-delivery-suite) | Beginner $8 monthly / $7 annual | Pro $25 / $21 |
| [Aftershoot Complete](https://aftershoot.com/complete/) | — | $55 monthly / $45 annual for Select + Edit + Retouch; Galleries separate |
| Hestia Studio | $40 monthly | one plan; no annual discount |

Hestia is therefore **not universally the cheaper sticker-price option**. It is below
HoneyBook Essentials and Pixieset Pro on month-to-month price, and below Aftershoot
Complete's AI bundle sticker, though [Aftershoot Galleries](https://aftershoot.com/galleries/)
is currently a separate 100 GB free launch product, so that comparison is not
like-for-like. Hestia is above HoneyBook/Pixieset entry tiers and far above Pic-Time
entry. The defensible claim is integrated value: one idempotent business-and-gallery
loop with no print commission, not “cheapest.”

The hosted beta subsidy defaults to one live xAI gallery per studio, capped at 150
images. A studio-owned key takes precedence and removes those caps; a deployment may
also disable the subsidy, leaving its configured live provider uncapped. Before public
pricing copy promises every module and AI inside $40, the owner must choose and
disclose a sustainable hosted-AI, BYOK, or metered policy. Hestia now exposes a
per-tenant tracked storage footprint and operator rollup without pretending that
metadata is a provider bill; quotas, dollar costs, and packaging remain product and
financial decisions.

## Caveats & open follow-ups

- Aftershoot currently advertises [100 GB of gallery hosting free at
  launch](https://aftershoot.com/galleries/) separately from Complete; future pricing,
  beta status, and storage should be re-verified before each packaging decision.
- HoneyBook's [July 2026 photographer release](https://www.honeybook.com/blog/the-new-honeybook-for-photographers-is-here)
  added native galleries and mini sessions; strategy must not rely on the old
  “weak galleries” characterization.
- Vendor revenue claims ("$2k in 30 days", "5× print sales") are promotional, not audited.
- The "Dubsado is best" verdict is one affiliate blogger; its workflow *depth* is
  independently confirmed.
- **Under-covered, worth a follow-up research pass:** Táve / Studio Ninja / Iris Works
  / Bloom / ShootProof internals, and deep G2 / Capterra / Reddit churn sentiment (to
  sharpen wedge messaging).

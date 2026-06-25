# Hestia Competitive Strategy & Build Roadmap

Derived from a verified competitive deep-research pass (2026-06; 24 adversarially
verified claims across HoneyBook, Dubsado, Pixieset, Pic-Time, Aftershoot, + Bain
SaaS-pricing data). Pairs with [`HESTIA-DOCTRINE.md`](HESTIA-DOCTRINE.md) (the *what/why*);
this doc is the *competitive landscape + what-to-build-next*.

## Executive summary

The market is collapsing into one fight. CRM incumbents (**Dubsado**, **HoneyBook**)
own booking → contract → invoice but have weak galleries and bolt-on AI. Gallery
players (**Pixieset**, **Pic-Time**) are weaponizing delivery into automated
print-revenue engines. And the acute threat — **Aftershoot** (June 2026) — turned
the best-in-class AI culling/editing tool into a full delivery platform (built-in
galleries, proofing, face search, print store; **$45/mo flat, unlimited processing**;
migration tooling *from* Pixieset/ShootProof/Pic-Time), collapsing edit → deliver →
sell into one app and invading Hestia's after-the-shoot loop.

**Hestia's moat is the only thing none of them have: the *whole* loop as one
idempotent system** — inquiry → contract → invoice → retention *and* gallery → AI →
offer — where the same vision signal that culls also auto-builds the offer, drafts
the album, and seeds the marketing pack. Aftershoot attacks from the AI-tool side
inward; Hestia owns the business-OS side it doesn't touch.

**The credibility hole:** Hestia today has no contracts, e-sign, scheduling,
questionnaires, payment plans, client portal, or automations. It is a strong
delivery + offer engine that a working photographer **cannot yet run their business
on.** Closing that booking-side gap — before Aftershoot extends upstream — is the
priority.

## The prioritized build roadmap

Each item is a vertical slice through the tenant → client → project → gallery spine.

> **Build status (delivered):** items 1–9, 11, 12 are shipped and on `main`. The full
> lifecycle runs end to end — visitor → inquiry → booking → contract → deposit →
> questionnaire → shoot → gallery → proofing → AI-curated offer → sale → fulfillment →
> retention. Only item 10 (deepen `vision`) remains, gated on the live xAI backend.

### Phase 1 — Contract-to-cash credibility (makes Hestia a real studio CRM)
1. ✅ **Contracts + e-signature** on the client/project spine (`crm`) — biggest table-stakes gap.
2. ✅ **Payment plans / deposits + milestones** on invoices (`invoices`/`payments`).
3. ✅ **Client portal** — one tenant-scoped URL aggregating contract + invoice + gallery + questionnaire.
4. ✅ **Questionnaires / intake forms** wired public inquiry → CRM lead (`studio` → `crm`).

### Phase 2 — Scheduling + automations (kill the busywork)
5. ✅ **Scheduler** with client self-booking + automated confirm/reminder emails (calendar seam).
6. ✅ **Workflow engine** — event-triggered (contract signed, payment paid, delivered, booked,
   appointment confirmed), with scheduled delays (the retention loop is the same engine + a delay).

### Phase 3 — Defend the after-the-shoot loop (vs Aftershoot / Pic-Time)
7. ✅ **Gallery proofing** + client favorites/comments (favorites feed `sales` curation).
8. ✅ **Sales automation campaigns** on `sales` — urgency-gated, time-limited sales **auto-curated
   from Hestia's own vision signal + the client's favorites** (the differentiator Pic-Time cannot match).
9. ✅ **Print-store fulfillment** seam (WHCC/Bay Photo class) — purchasable offers settle to a lab order.

### Phase 4 — AI compounding + retention (the durable moat)
10. **Deepen `vision`** to credible cull/dup/blink-rejection parity + custom AI style
    profiles gated by tier. *(Remaining — needs the live xAI backend to be meaningful.)*
11. ✅ **Retention/upsell automations** (anniversary re-book, review requests, welcome) — delayed rules.
12. ✅ **Mobile-responsive** client + photographer surfaces.

## Where AI is a wedge vs a gimmick

- **Genuine & defensible:** AI wired *into the revenue loop* — auto-curating sellable
  packages from the gallery's vision signal, album drafts (model proposes, code
  validates), marketing copy at the friction point. Cross-module compounding no point
  tool spans; **this is the moat.**
- **Commodity / gimmick:** standalone "AI editing styles" as a checkbox; any AI that
  doesn't cut admin or drive a sale. High-volume culling is now a table-stakes *floor*
  (Aftershoot owns it) — Hestia needs parity, not a pitch.

## Pricing & packaging

Bain: **hybrid pricing wins** — a flat base + a light AI usage/feature meter (~65% of
SaaS vendors adding AI went hybrid; per-seat breaks for AI agents). Aftershoot ($45
flat, AI gated by tier) and Pixieset (storage-tiered, **15% print commission** as the
upgrade lever) bracket the market; HoneyBook's Feb-2025 51–89% price hike drove churn
*to* Dubsado. Annual ≈ 20% discount is segment-standard.

**Hestia tiers** (map onto the existing `subscriptions` module):
- **Solo Studio** — full CRM + booking + galleries + invoicing + baseline AI culling.
- **Studio Pro** — + full automations, deeper AI editing/album/marketing, more storage, custom AI profile.
- **Agency / Team** — + seats/roles, multiple AI profiles, priority.
- **Levers:** flat base (anchor) · AI overage credits *only* for heavy/variable runs ·
  **20% annual-commit discount** · **wedge: low/zero print commission** funded by
  subscription (Pixieset/Pic-Time's 15% tax is a resented churn driver).

## Caveats & open follow-ups

- Aftershoot Galleries is **beta** — its pricing/storage will move; re-verify before
  keying decisions off it.
- Vendor revenue claims ("$2k in 30 days", "5× print sales") are promotional, not audited.
- The "Dubsado is best" verdict is one affiliate blogger; its workflow *depth* is
  independently confirmed.
- **Under-covered, worth a follow-up research pass:** Táve / Studio Ninja / Iris Works
  / Bloom / ShootProof internals, and deep G2 / Capterra / Reddit churn sentiment (to
  sharpen wedge messaging).

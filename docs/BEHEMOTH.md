# Hestia — the behemoth blueprint

One multi-tenant SaaS that absorbs the **best of all six** photography projects as
in-process modules. Not six services behind a shell — one app, one identity, one
bill, one database, modules composing around the studio's real workflow.

## The six → modules

| Module | Best-of (source) | Status |
|--------|------------------|--------|
| Studios · auth · API keys | (control plane) | ✅ shipped |
| Galleries · object storage | **mise** delivery | ✅ shipped |
| Vision (cull · keyword · hero) | **argus** | ✅ shipped (`mock`/`xai`) |
| Sales · idempotent client offers | **plutus** | ✅ shipped |
| CRM: clients + projects | **mise** back-office | ✅ shipped |
| Invoicing + payments (mock/stripe) | **mise** invoices + **plutus** checkout | ✅ shipped |
| Album designer (model proposes, code validates) | **mnemosyne** | ✅ shipped |
| **Marketing content (shot lists, captions, campaigns)** | **dionysus** | ✅ shipped |
| Product photography (packshots, variants) | **aphrodite** | ▢ next |
| Public studio site / booking | **mise** site | ▢ |

## Why this order

1. **CRM backbone first.** Galleries currently float free. Clients → projects →
   galleries is the spine the whole studio OS hangs off; invoices, albums,
   campaigns, and the public site all attach to a client/project.
2. **Revenue next.** Stripe checkout on offers + client invoices — turn the
   workflow into money (build on `billing.py`).
3. **AI value-adds.** Album designer, marketing content, product photography —
   each a module behind the same vision/storage/tenant plumbing already in place.
4. **Public surface last.** A studio site + booking form that feeds the CRM.

## Architectural rules (unchanged)

- One app, in-process modules — no service hops.
- Tenant-scoped everything; never leak across studios.
- Pluggable seams: vision (`mock`/`xai`), storage (`local`/`s3`), and now
  payments (scaffold → Stripe).
- Idempotent money paths (offers already; invoices/checkout next).
- Each module ships as its own slice: data model → module → routes → templates →
  tests → PR. Green CI + dogfood before merge.

## The data spine (after this PR)

```text
tenant (studio)
  └─ client
       └─ project (shoot_type, status, event_date)
            ├─ gallery ── images ── image_analyses
            │      └─ offer (idempotent client link)
            └─ invoice (draft → sent → paid, public pay link)
```

Everything added later (invoice, album, campaign, packshot) attaches to a
**project** or **client** — so the behemoth grows without re-plumbing identity.

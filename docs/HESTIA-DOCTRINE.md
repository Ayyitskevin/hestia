# Hestia Doctrine

The canonical statement of what Hestia is, why it exists, and how it must be built.
When any other doc, comment, or instinct conflicts with this file, this file wins.

---

## What Hestia is

**Hestia is the AI-native studio operating system for photographers and creative
studios.** It is an independent, commercial SaaS product with its own codebase,
identity, architecture, database, and business engine.

It owns the entire studio revenue loop — from a stranger on the internet to a paid,
retained client — and embeds AI at the exact points where it saves time or makes
money. One FastAPI + Jinja2 + HTMX + SQLite app. A modular monolith.

## What Hestia is NOT

- **Not a wrapper** around older repos.
- **Not an orchestrator** that calls sibling services over HTTP.
- **Not a repo merger** — it does not import or vendor sibling code.
- **Not a Frankenstein** of copy-pasted services ("Argus-in-Hestia, Plutus-in-Hestia").
- **Not six products in one repo.** It is *one* product with several modules arranged
  around *one* revenue workflow and *one* data spine.
- **Not generic purple SaaS sludge.** It is a warm, photographer-native studio command center.

The older repos — **Mise, Argus, Plutus, Mnemosyne, Dionysus, Aphrodite, Athena,
Midas** — are **design DNA, not dependencies.** Study their patterns, keep their
hard-won lessons, rebuild only the essence that belongs in Hestia.

## The SaaS business thesis

The goldmine is **not** "AI photo tools." Tools are commodities. The goldmine is
**owning the client-to-cash operating system** for a studio — the warm, daily home
where a photographer books, delivers, sells, invoices, and retains — and then
embedding AI where it compounds. Whoever owns the workflow owns the customer.

Think: **HoneyBook + Pixieset + Pic-Time + a Lightroom culling assistant +
Notion-lite + Stripe checkout + an AI sales assistant** — but simpler, warmer, more
photographer-native, and more automation-driven.

Every module must answer at least one:
1. Does it help the photographer **book** more clients?
2. Does it help them **deliver** faster?
3. Does it help them **sell more** after delivery?
4. Does it **reduce admin drag**?
5. Does it **improve the client experience**?

If a feature answers none of these, shelve it.

## The canonical customer

A working **solo or small-studio photographer** (weddings, portraits, events,
commercial/product) who today juggles a booking tool, a gallery host, a separate
invoicing app, a spreadsheet CRM, and manual culling. They want one warm,
intelligent place that runs the business so they can shoot. The buyer and the daily
user are the same person; the product must feel personal, not enterprise.

Secondary: a **small team/agency** studio with a few photographers and an
owner/admin who needs roles, oversight, and reporting.

## The canonical workflow (the spine everything hangs off)

```text
visitor → inquiry → client → project → gallery
  → AI culling / metadata → offer → album draft → marketing pack
  → invoice → payment → retention / upsell
```

Public to paid to repeat, in one app. Every module attaches to this spine —
nothing floats free of a `tenant → client → project → gallery`.

## Module boundaries

**Core platform** (shared by every studio module):
`tenants` · `auth` · `csrf` · `db` (+ migrations) · `storage` · `jobs` ·
`audit`/activity · `billing`/`subscriptions` · `payments` · `email`/outbox ·
settings · admin/ops · `obs` (structured logging).

**Studio modules** (the revenue workflow):
| Module | Owns |
|--------|------|
| `studio` | public studio profile, inquiry capture, lead conversion |
| `crm` | clients, projects, project lifecycle |
| `galleries` | galleries, image upload, delivery, publication |
| `vision` | culling, metadata, keywords, hero scoring |
| `sales` | print / album / product **offer engine** (idempotent client links) |
| `albums` | album draft engine (model proposes, code validates placement) |
| `content` | marketing packs, captions, shot lists, campaign drafts |
| `products` | commercial / product-photo variant planning + rendering |
| `invoices` | invoices, public pay links |
| `pipeline` | gallery/project automation as a registry of internal steps |

**Rule:** a module is a slice of *one* product (a Vision module, an Offer module),
**never** a port of a sibling service. Modules share the platform; they do not each
re-implement identity, billing, or storage.

## The tenant / data spine

Hestia is **SaaS-native from the foundation.** Every meaningful row is
**tenant-scoped**. A tenant **is** a studio, and every studio has: users + roles,
plan/subscription state, a public profile, settings, a storage namespace, and
billing state.

```text
tenant (studio)
  ├─ users (roles), api keys, sessions
  ├─ subscription / plan / billing state
  ├─ studio profile (public site) + settings
  └─ client
       └─ project (shoot_type, status, dates)
            ├─ gallery ── images ── analyses
            │      └─ offer  (idempotent client link)
            │      └─ album draft · product set · content pack
            └─ invoice (draft → sent → paid, public pay link)
```

No single-studio shortcuts unless explicitly marked local-dev/demo only. Never leak
a row across tenants; every query is keyed by `tenant_id`. Tenant isolation is a
tested invariant, not a hope.

## AI philosophy

AI is a **revenue / operator layer, not a gimmick.** Every AI capability is a
provider-backed **seam**:
- **`mock` by default** — deterministic, no keys, runs in CI and demos.
- **Real provider later** (xAI/OpenAI/…), selected per backend by env.
- **The model proposes; code validates.** An LLM suggests order, copy, or scoring;
  Hestia's code enforces the invariants (e.g. every photo placed exactly once).
- **Schema-validated** outputs, **safe error handling** (a provider miss degrades
  to the deterministic path; it never 500s a request), and **cost tracking** as AI
  usage grows.

Add a backend; never fork the caller.

## Money-link / idempotency philosophy

Client-facing money links are **idempotent by construction**:
- Re-processing a gallery **reuses** the single offer token for that gallery — never
  a duplicate client link. (This is the exact bug the real Plutus had; Hestia exists
  to not have it.)
- Re-sending an invoice does not create duplicate payment records; settling is
  idempotent (a double webhook never double-settles).
- Every public token (`/s/{slug}/{token}`, `/pay/{token}`, reset/verify links) has a
  clear uniqueness and lifecycle policy: scoped, single-purpose, and (for secrets)
  hashed at rest, single-use, and expiring.

## Design / UX personality

A **premium studio command center**: warm, calm, photographer-native — the "hearth"
of the studio. FastAPI + Jinja2 + HTMX, simple hand-written CSS, a cream/ember/sage
palette. Beautiful on the surface, **boring under the hood**: explicit SQL,
forward-only migrations, durable jobs, no premature React, no microservice sprawl,
no enterprise-architecture cosplay. Batman needs a Batcave, not a Jira committee.

## Lessons from each prior repo (keep) — and traps (don't copy)

| Repo | Lesson to **keep** | What **NOT** to copy |
|------|--------------------|----------------------|
| **Mise** | Native gallery delivery + a public studio site that captures leads | Single-tenant model + shared-local-disk storage (the consolidation target, not a pattern) |
| **Argus** | A clean vision engine: cull / keyword / hero scoring behind a provider seam | Its standalone SaaS layer, job worker, and Grok client as a *service* — rebuild the essence in-process |
| **Plutus** | Print/album **offer** engine with clean webhook-in / offer-out shape | **Non-idempotent links** — it mints a fresh client URL every call. Never. |
| **Mnemosyne** | Album-draft engine; "model proposes, code validates" placement | Half-duplicating a print CTA — delegate selling to the one Offer engine |
| **Dionysus** | Marketing content (shot lists, captions, campaigns) as a module | "AI" that is deterministic templates with no real model and no seam |
| **Aphrodite** | Product/packshot variant planning to marketplace specs | Treating it as a *separate* product/customer — it's a Hestia module on the same spine |
| **Athena / Midas** | Whatever operator/financial discipline they encode | Any runtime coupling; absorb the lesson, not the service |

The pattern across all of them: **identity, billing, and storage were
re-implemented 4–6 times.** Hestia's entire reason to exist is to implement those
**once** and let the modules share them.

## North star

Hestia should become the system a working photographer **pays for** because it helps
them **book, deliver, sell, invoice, and retain** clients from one warm, intelligent,
automated studio OS. Be rigorous, product-minded, and commercially ruthless. Favor
clear customer value over architectural cleverness. This is a SaaS goldmine, not a
science fair.

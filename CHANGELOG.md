# Changelog

All notable changes to Hestia are documented in this file. The format follows [Keep a Changelog](https://keepachangelog.com/) and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Security

- Centralized the private-surface path registry across response hardening, access-log
  redaction, `robots.txt`, hosted preflight, and CI. Proposal bearer tokens are now
  redacted in real request logs and proposal pages carry an explicit `noindex` meta tag.

## [1.0.0] - 2026-07-02

The go-live release. 0.1.0 froze the feature set; 1.0.0 is that product made
launchable — security-hardened end to end, wired for a real box (TLS, deploys,
backups, monitoring), documented for the founder who has to run it, and polished
for the photographer's first hour. Validated by a full first-owner smoke against a
live boot: signup → verify → preset → publish → gallery → AI offer → invoice →
client payment → launch checklist complete, with zero paper cuts.

### Added

- **Day-0 welcome email** the moment a studio verifies, naming the three moves from
  empty to client-ready (preset → publish site → first gallery) with direct links;
  sent inside the single-use verify transaction so it can never double-send, covering
  both self-serve signup and private-invite paths.
- **Branded error pages.** A mistyped or expired link gets a warm 404 in the Hestia
  voice; a rare crash gets a friendly 500 while the stack trace goes only to server
  logs. Both `noindex`; API and webhook routes keep their machine-readable JSON.
- **Marketing share image** (Open Graph / Twitter card) so links to the landing and
  beta pages unfurl properly in DMs, group chats, and social.
- **Founder documentation set:** security playbook (`docs/security.md`), support
  first-response kit with ready-to-send replies (`docs/support.md`), beta onboarding
  runbook with the first-14-days email arc and a day-7/day-30 retro template
  (`docs/beta-onboarding.md`), production operations cadence with external uptime
  monitoring (`docs/operations.md`), deploy wiring (`docs/deploy-wiring.md`), 7-day
  launch checklist (`docs/launch-checklist.md`), and a 60-second Quickstart at the
  top of the README.

### Fixed

- **No more stranded signups.** An account whose verification email was lost or
  expired had no way in (no resend exists); completing a password reset now also
  verifies the account, since a consumed reset link proves the same mailbox
  ownership. "Forgot password" is the universal recovery path.

### Security

A five-wave hardening pass over attack surface, tenant isolation, auth, payments,
and monitoring — each invariant landed with a regression test:

- **Strict Content-Security-Policy** with a fresh per-request nonce: `script-src` is
  nonce-only (no `'unsafe-inline'`), `object-src 'none'`, `frame-ancestors 'none'`,
  `base-uri`/`form-action 'self'`; every inline event handler was refactored to a
  single nonce-carrying delegated listener. Plus `Strict-Transport-Security` (1 year),
  `Permissions-Policy` (camera/mic/geolocation/payment/USB all denied), and
  `Cross-Origin-Opener-Policy: same-origin` on every response.
- **Password KDF raised to PBKDF2-HMAC-SHA256 at 600k iterations** (OWASP-current),
  with transparent re-hash-on-login for any account stored at a weaker work factor.
- **Uploads bounded end to end:** per-image cap (75 MB) enforced with a bounded read
  so an oversized body can't OOM the box, and public free-text fields length-capped
  at the data layer.
- **Tenant-matched joins in the AI/vision read paths** — a stray cross-tenant image
  id is dropped by the join, not surfaced (defense-in-depth on top of scoping).
- **Stripe settlement mode-guard:** only a `payment`-mode checkout event can settle
  an invoice, so a subscription-mode event with hostile metadata can't mark client
  invoices paid.
- **Supply-chain hygiene:** `cryptography` floor raised past known CVEs and an
  advisory `pip-audit` step added to CI.
- **Audit completeness:** password resets now land in the audit trail alongside the
  existing money, media, and access events.

### Infrastructure & Operations

- **On-demand TLS for tenant subdomains:** each `{slug}.domain` gets its own
  certificate on first hit via Caddy on-demand issuance gated by Hestia's
  `/internal/tls-check` (only real tenants and verified custom domains get certs) —
  no wildcard cert, no DNS-provider credentials.
- **Deploy activation:** production compose wiring (app + Caddy + daily backup
  sidecar), a pre-tuned `.env.production.example`, and the hosted preflight gate
  runnable against the live domain.
- **CI privacy teeth:** every client-token template must carry `noindex` and
  `robots.txt` must disallow every token prefix — enforced on every run, plus a
  live-domain probe in preflight.

## [0.1.0] - 2026-07-02

Initial release. Hestia is a hosted, AI-native, flat $40/month operating system for photography studios. It owns the whole business in one place — visitor to inquiry to client to gallery to AI cull to print/album offer to invoice to payment to retention — with a public studio site and booking front door, a CRM and delivery back office, and an operator control plane for running the hosted service. Every studio is a fully isolated tenant; client-facing links are password-free capability tokens; the background worker handles the follow-ups so the photographer doesn't have to.

### Added

**Studio site & booking**
- Public studio site at `/studio/{slug}` — headline, about, active packages and prices, featured testimonials, a booking CTA, and published mini-sessions; unpublished studios show a coming-soon holding page instead.
- Public reviews wall at `/studio/{slug}/reviews`.
- Bookable session-type menu (kind, duration, price, deposit) with soft-archive toggle and delete, driving a public booking page.
- Weekly availability windows plus guardrails (minimum-notice hours, buffer minutes) that generate concrete open slots for self-serve booking.
- Mini-session drops: create with price/deposit/duration, add fixed time slots, publish/unpublish/archive, remove open slots — each with its own public page.
- Session scheduling: propose sessions with multiple time options, manage through confirm/cancel/complete/no-show, block off personal busy time, and view the schedule plus an upcoming agenda.
- Subscribe-able studio calendar feed (`.ics`) via a regenerable token, plus per-session add-to-calendar downloads for owner and client.
- Email on every new inquiry, booking request/confirmation, and mini-session claim, routed to the studio's contact email or the owner login.
- Word-of-mouth attribution: sharing `/studio/{slug}?ref={code}` tags any resulting inquiry back to the referring client.
- Public marketing pages: landing, pricing (flat monthly price and trial length), and per-niche demo tours (wedding, portrait, food, real estate).
- Clients submit an inquiry (name, email, message, shoot type, event date, optional package interest) and instantly become a CRM client and lead project.
- Clients book self-serve: pick a type and either grab a real open slot (auto-confirmed with confirmation email and add-to-calendar) or request a free-text time; deposit-carrying types raise a deposit invoice and route to pay.
- Clients claim an open mini-session slot from a public drop page and get a confirmed session (or pay a deposit first).
- Clients self-manage a booking from its link: view it, confirm a time option, reschedule to another open slot, cancel, and download the session `.ics`.

**Client & project workflow**
- Client book with name, email, phone, and notes, organized by normalized tags/segments, showing each client's project count and collected lifetime value.
- Bulk CSV import with foreign-header auto-mapping (Full Name, Mobile, Labels…), skipping blank rows and email duplicates so re-importing is safe; CSV export honoring the active tag filter.
- Free-text search across clients (name/email) and projects.
- Client detail page with a chronological timeline of everything (projects, contracts, questionnaires, sessions, invoices, payment plans, delivered galleries), referral link, credit balance, and in-app message history.
- In-app client email from a saved template with the studio signature appended, plus a tag/segment broadcast with `{client}` filled per recipient.
- Printable client account statement (billed, paid, and outstanding), shared in the CRM and the client portal.
- Projects (shoot type, status, event date, notes) with a per-project workspace aggregating galleries, invoices, payment plans, contracts, questionnaires, sessions, content packs, tasks, and files.
- Pipeline stages (lead → booked → shooting → delivered → archived); marking booked auto-awards any referral credit, lays down the shoot-type checklist, and fires a booking automation.
- Pipeline board grouped by stage with per-stage counts and revenue collected.
- Per-project tasks (add/toggle/delete) and one-click apply of the reusable shoot-type checklist (idempotent).
- Attach, download, and delete project reference files.
- Questionnaires: title plus ordered prompts (optionally from a template), sent by email, tracked draft → sent → completed → void, saveable as a template; clients fill from a public tokenized link.
- Contracts: draft (title, terms, named signer), optionally from a template, sent for signature, tracked draft → sent → signed → void, saveable as a template; clients review and sign by typing a name (name, timestamp, and IP captured).
- Proposals: a package-backed bundle of a draft agreement and a booking/deposit invoice behind one shareable link, terms auto-generated from the package; clients accept, sign, and pay from that single link.
- Proposal funnel: view counts, per-proposal next-action guidance, an open-value follow-up list, dashboard conversion metrics (sent → accepted → paid, avg time to book), and manual + auto reminders (capped).
- Owner "today" attention queue: new leads, unpaid invoices, upcoming and to-confirm sessions, galleries ready to deliver, album change requests, unsigned contracts, unfilled questionnaires, and proposal follow-ups.
- Deterministic, explainable hot-lead scoring (source, freshness, event date, email, sessions, proposals, retainer intent) with a suggested next action.
- Reconnect list of past clients gone quiet (~10 months since last project) with an email to reach them.
- Owner "what needs you" digest emailed on demand and on a weekly cadence, with a per-studio opt-out.
- Background sweeps chase unsigned contracts and unfilled questionnaires with cooldown-gated reminders.

**Galleries & AI**
- Client galleries: create, bulk upload, and publish for a shareable, optionally PIN-gated client view.
- AI vision pass tagging every frame with keywords, shot type, keeper and hero scores, blink/exposure/sharpness signals, and descriptive alt text.
- Per-frame AI read on the gallery detail page (keywords, shot type, keeper flag, soft/dark/bright flags).
- One-click AI cull hiding near-duplicate clusters and likely blinks — fully reversible, nothing deleted.
- One-click quality cull hiding likely technical rejects (soft, under- or over-exposed), reversible per frame.
- Hide or restore any individual frame from the client gallery and delivery.
- AI hero/cover suggestions and one-click cover set.
- Free-text AI style profile that biases keeper and hero scoring toward the studio's look (higher plan tiers).
- Catalog-wide search across galleries by keyword, shot type, keepers-only, or clean-only, with keyword and shot-type facet clouds.
- Clients heart favorites, leave per-photo comments, and finalize picks with a one-way submit that notifies the studio once.
- Selection packet of favorites plus notes, downloadable as a Lightroom-ready selects list or a plain-text handoff.
- AI album draft arranging frames into hero-led spreads while placing every non-culled photo exactly once; refine by overriding a spread's hero and reordering spreads.
- Album client review via an unguessable link: page the spreads and approve or request changes with a note.
- Private, login-free delivery link that emails the client, with a settable expiry and one-click rotation that revokes the old link.
- Clients download originals individually, the whole set as one streamed zip, or just their favorites as a zip.
- Trigger a gallery's AI pipeline run and poll status over a JSON API (session cookie or bearer API key), and watch progress in a live stepper UI.

**Sales, invoicing & payments**
- Invoices with a flat amount or itemized line items (including negative/discount lines), client and project attached, and sales tax added automatically at the configured rate.
- Send invoices (pay link plus a personal note), tracked draft → sent → paid/void, with a note and a duplicate-for-repeat-billing action.
- Record offline payments (cash, check, bank transfer) and email a paid receipt however the invoice settled.
- Accounts-receivable (outstanding and overdue totals), status/overdue filters, and an on-demand past-due reminder; overdue invoices are also chased automatically up an escalating dunning ladder by the worker.
- Clients pay from a public tokenized link via Stripe Checkout (or the mock provider), with a printable receipt.
- Clients apply a promo code or redeem a gift card before paying (gift cards work across multiple partial payments); an invoice fully covered by a card/discount, or a $0 invoice, settles at checkout without hitting a payment provider.
- Deposit-plus-balance payment plans whose installments are individually payable invoices, sent to the client at once, with progress tracking and void.
- Recurring retainer/subscription billing (weekly/monthly/yearly) auto-generated and emailed on cadence, with pause/resume/delete.
- Reusable service packages (price + deposit) that pre-fill new invoices and payment plans.
- Each processed gallery mints one idempotent, shareable print/album offer curated from the AI hero picks and keeper count, plus a live "Your Favorites" package built from the frames the client hearted (recomputed at render time, dropping since-culled frames).
- Time-limited gallery sales campaigns (headline, discount, deadline) that discount the offer live and show an urgency countdown — one active per gallery, endable early; sell-readiness scoring can auto-launch and email post-delivery campaigns on a cooldown.
- Clients order a bundle, creating a paired order and invoice routed to checkout; the price is recomputed server-side from stored bundles with any active sale applied.
- Paid print orders are automatically submitted to a print lab (mock, or a real WHCC/Bay-class lab over HTTP) via the durable job queue, with every attempt and its status shown on the gallery.
- Stored-value gift cards with auto-generated bearer codes, optional expiry and note, and activate/deactivate; visitors buy one from a studio's public page (paying its invoice, card emailed to the recipient) and anyone can check a card's balance by code.
- Promo/discount codes (percent or fixed amount, optional usage limit and expiry) with toggle and delete.
- Expense tracking (manual or CSV bank-export import with column mapping and dedupe) and real P&L — collected revenue minus expenses, overall and per project.
- Finance reports: A/R aging, expense breakdown, monthly P&L trend, sales-tax collected and by-period, booking funnel, lead sources, gallery-sales conversion, and top clients; CSV export of expenses, income, and tax-by-month.

**Client portal & retention**
- One-click branded, password-free client portal per client (unguessable link, idempotent), with on-demand rotation that instantly revokes the old URL.
- Portal action room: contracts to sign, session times to pick, albums to review, questionnaires to answer, installments/invoices to pay, galleries/files to download, and a review to leave — with live to-do and ready counts and a billed/paid/outstanding statement.
- Clients download studio-shared project files (always as an attachment) and message the studio from the portal (delivered as an owner alert with the client's reply-to).
- Testimonials: request via an unguessable review link; clients submit a clamped 1–5 star rating and a few words with their name pre-filled; owners feature or hide returned reviews, and featured ones render on the studio home and reviews page.
- Growth opportunities: a ranked list of happy clients (identified by paid invoices and delivered galleries), each labeled ready for a review or referral ask, with one combined ask email per client in a single click.
- Referrals: every client gets a shareable link, referred inquiries are attributed to the referrer, and when a referred lead books the referrer earns a redeemable credit (owner marks it redeemed).
- Automations: event-triggered "when X happens, email the client" across eleven lifecycle triggers, optionally delayed a set number of days; one-click retention recipes (review request, anniversary re-book, post-booking welcome, session prep, cancellation win-back, booking follow-up); enable/disable/delete with a run log of every send, skip, and outcome.
- Customizable subject and body for the review-request, growth-ask, and print-offer emails, with built-in defaults.

**Hosted platform & billing**
- Start the hosted studio on the flat $40/month plan through Stripe Checkout (subscription mode) with an automatic 14-day trial; manage card and subscription via the Stripe billing portal; cancel to downgrade to the free Beta plan in one click.
- Account and billing pages showing plan status, subscription state, hosted studio URL, and custom-domain setup.
- Custom domains (attach with a DNS verification token and target) and premium per-tenant subdomains (`{slug}.{domain}`), resolving to the public studio site with no separate in-app DNS wiring.
- Stripe webhooks drive the full subscription lifecycle: checkout completion activates the plan, `subscription.updated` syncs trial → active and past_due, `subscription.deleted` downgrades to canceled Beta.
- Onboarding presets for four niches (wedding, portrait & family, food & beverage, real estate) that seed site copy, booking types, service packages, and an intake questionnaire, optionally with a demo client and project; first-run owners are routed into onboarding until a preset is applied, then to the dashboard.
- Public beta-interest forms (`/beta`, `/interest`) with validated email and source/landing-path attribution, deduped by email; private, time-limited invite redemption that spins up a studio; self-serve signup (when enabled) with email verification; per-niche demo tours that deep-link to the live seeded demo studio once published.
- Operator admin: master-token sign-in, create studios (tenant + owner user + minted API key), set shoot type, mint additional API keys, and verify/reset custom domains.
- Operator launch kit: invite links, launch-readiness checks, milestones, a revenue pipeline, cohort pulse, and a prioritized operating checklist (CSV-exportable); beta-interest summary with individual or oldest-first batch invites that never re-send to invited or converted contacts.
- Operator launch nudges (3-day cooldown) and on-demand launch digest, plus a cross-studio trial-conversion cockpit (activation %, trial state, churn risk, next action) and per-studio conversion timeline.
- Seed four founder demo studios (one per niche) with preset setup, a published site, and a processed showcase gallery whose AI cull hides a blink and a duplicate, delivery enabled and album shared for review.
- Automatic, cooldown-protected outbound sweeps: trial-ending nudges and past-due card-dunning emails without any manual click.
- A durable SQLite-backed worker drains a job queue and runs periodic sweeps for overdue-invoice, unsigned-document, and stalled-proposal reminders, owner and launch digests, trial nudges, dunning, gallery-sale campaigns, and recurring invoices.
- Operator system health (version, tenant count, queue stats, migrations, backend seams, config warnings), failed/stale job inspection with safe requeue, and a tenant data-integrity overview.

### Security & Privacy

- **Tenant isolation everywhere.** Every CRM, questionnaire, contract, proposal, gallery, money, campaign, automation, and subscription query is tenant-scoped; optional client/project parents are validated to the tenant before write, read joins are tenant-matched, and a stray cross-tenant id is dropped rather than surfaced. Storage blobs are tenant-prefixed so images never leak across studios, and webhook subscription updates guard on tenant existence so foreign or bogus Stripe metadata can't write orphan rows or 500 the endpoint.
- **Password-free capability tokens for client surfaces.** Delivery (`/d/{token}`), album review (`/a/{token}`), per-image media (`/media/{token}`), portal, pay, sign, questionnaire-fill, and proposal-accept links are gated only by unguessable tokens — the token is the credential. Minting is idempotent and race-safe (claim-before-act), rotation instantly revokes the prior link, delivery links honor an expiry gate (410 past the date), and these routes are deliberately CSRF-exempt but rate-limited.
- **Private surfaces stay out of search.** `robots.txt` disallows every token-gated surface (portal, delivery, pay, sign, gallery, offer, questionnaire, invite, verify/reset, calendar, media) on top of per-page `noindex` meta, while keeping marketing, studio, and booking pages indexable; CI enforces both invariants and a live-robots probe.
- **Access-log token redaction.** The credential-bearing tail of every client-token and media path is redacted to just the route prefix, so working bearer tokens are never persisted to logs.
- **Media can't execute on our origin.** Inline images are clamped to a raster-type allowlist so a stored `text/html` or SVG "image" downloads as octet-stream instead of running (stored-XSS defense); the `/media` path serves only published, non-hidden, non-culled frames, and the enumerable storage-key path is owner-only (403). Culled/hidden frames never resurface in client galleries, delivery, album review, or offer thumbnails, even by direct image id.
- **Stripe webhook: verified, replay-safe, idempotent.** The `Stripe-Signature` header is checked with HMAC-SHA256, a constant-time compare, and a timestamp-tolerance replay window before any invoice is marked paid; an unconfigured secret returns 503; redelivered events are no-ops. Invoice settlement uses an atomic status-guarded UPDATE plus a rowcount barrier, so at-least-once retries or a double-click never double-settle, double-audit, or double-fire `invoice.paid`.
- **One-way client actions are first-write-wins.** Contract sign, questionnaire completion, proposal accept, booking confirmation, selection submit, album approval, and testimonial submit each stamp exactly once under a status guard; a signed contract can't be re-signed or voided, a completed questionnaire keeps its answers, and an approved album locks against regeneration and spread edits.
- **Server-authoritative pricing.** Order prices are recomputed server-side from stored bundles (or the live favorites package) with any active sale reapplied — the client's posted price is never trusted. Discount and gift-card applies run under `BEGIN IMMEDIATE` with guarded UPDATEs and a UNIQUE redemption ledger, are mutually exclusive to keep revenue and tax correct, and gift cards are issued exactly once and only after payment.
- **CSRF on the owner/operator UI.** All session-cookie form POSTs carry a session-bound CSRF token; the signature-verified Stripe webhook and read-only media/health routes are the deliberate exemptions, and cookieless public capture POSTs are CSRF-exempt but rate-limited.
- **Per-IP rate limiting** on public capture (inquiry, booking, mini-session claim, portal message), interest/beta/signup, login/admin-login/password-reset, checkout/discount/gift-card apply/offer-order/gift-card buy, reschedule/cancel, and public zip downloads — deterring abuse and gift-code enumeration; a single gift-card purchase is capped at $10k with amount/email validation.
- **Non-enumerating auth.** Password reset returns an identical response whether or not the email matched and logs the user out everywhere on success; beta invite tokens and tenant API keys are hashed-at-rest with a pepper, and invites are single-use and expire after 7 days.
- **Injection-safe outputs.** Every CSV export (client book, finances, launch) neutralizes spreadsheet formula injection by prefixing cells that start with `= + - @` or a control char; calendar output is RFC-5545 escaped so owner-entered titles and locations can't inject extra calendar lines; project and portal file downloads are always attachment disposition with sanitized filenames.
- **Fail-safe AI.** The vision provider coerces malformed or missing LLM fields to safe defaults, so one bad frame response never strands a pipeline run.

### Infrastructure & Operations

- **Daily backups and a drilled restore.** WAL-safe online SQLite backups run daily with rotation and fail loudly (container crash-loop); restore is a two-command drill with WAL-liveness refusal, a pre-swap integrity check, and a retained pre-restore copy.
- **Hosted preflight go-live gates.** Preflight blocks launch unless SaaS mode, HTTPS public URL, wildcard domain, secrets, the locked $40 price and 14-day trial, Stripe and SMTP config, invoice-payments backend, data/media volumes, and the Caddy wildcard are all correct, and it runs live `/healthz`, `/readyz`, and `robots.txt` probes. It hard-fails mock invoice payments (which would flip invoices to paid and fulfill orders with nothing charged) and fails when the newest backup artifact is stale. Self-serve signup is feature-flagged off by default, keeping onboarding invite-only.
- **Durable job queue.** Work is claimed atomically so a job never double-runs, retries with exponential backoff, reclaims crash-orphaned jobs on a cadence, and refuses to requeue a genuinely in-flight job. Automation emission and the booking lead/appointment/deposit-invoice creation each commit inside one caller-owned transaction, so a trigger and its follow-up land together or not at all.
- **Concurrency-safe money and booking.** Recurring billing atomically claims a due profile (advancing `next_run_at` in one guarded UPDATE) and commits before emailing, so concurrent sweeps or a failed SMTP send never double-bill (first run floored at today); print fulfillment pre-claims each order on a UNIQUE latch so an at-least-once queue never double-submits a physical order, with lab errors captured as row status rather than raised. Public booking and reschedule take SQLite's write lock and re-check slot availability before confirming, and mini-session claims flip the slot with a status-guarded UPDATE — no double-booking under concurrency.
- **Reminder sweeps claim-before-send.** Contract, questionnaire, and proposal reminders, growth asks, gallery sales-campaign emails, owner and launch digests, trial-ending nudges, and past-due dunning all claim atomically and share audit-ledger cooldowns between the manual admin action and the automated worker, so a studio or client is never double-emailed; proposal auto-follow-ups cap at 3 and campaign emails honor a 14-day per-gallery cooldown.
- **Traceability and hardened responses.** Every response carries `nosniff`, `SAMEORIGIN` frame options, a strict referrer policy, and a per-request `X-Request-ID`.
- **Streaming stays bounded.** Zip delivery streams one file at a time (`ZIP_STORED`) so a multi-GB gallery can't OOM the server, and client CSV import authenticates before reading the body, enforces a 5MB cap, and turns a malformed/binary file into a friendly error.

[1.0.0]: https://github.com/Ayyitskevin/hestia/releases/tag/v1.0.0
[0.1.0]: https://github.com/Ayyitskevin/hestia/releases/tag/v0.1.0

# Hestia human decision queue

- **Prepared:** 2026-07-17
- **Evidence base:** `465c6b45abdb46e9ddce64d9d4b8fdbcd5a6fa1d`
- **State:** recommendations only; no choice below is approved by this document
- **Purpose:** turn launch-critical money, security, schema, and durability holds
  into small decisions that unlock bounded implementation slices

Hestia can remain one modular monolith and preserve every shipped workflow. These
decisions define the contracts at its riskiest seams; they do not authorize a deploy,
live-data migration, external account change, or public launch.

## Decision summary

| ID | Owner decision | Recommended choice | Boundary held until approval |
|---|---|---|---|
| D1 | What does $40 include for live gallery vision? | Flat $40 core; one trial live gallery analysis up to 150 images; studio vision BYOK afterward during beta | Public gallery-vision copy and trial enforcement |
| D2 | Who receives a studio client's invoice payment? | Stripe Connect direct charge to the studio, card-only, no Hestia application fee | Live client-invoice Checkout and fulfillment |
| D3 | What authorizes anonymous image bytes? | Authorization inherited from proofing, delivery, offer, or album-review surface | Media/PIN security behavior |
| D4 | How should the two known migration-0065 shapes be treated? | Audit every real DB snapshot; support both recognized shapes for v1; do not normalize without evidence | Any live-schema remediation |
| D5 | What proves DB and gallery media exist off-box? | Local serving for beta plus verified daily non-deleting off-site copy, provider version retention, and a freshness receipt | Durability claim and launch pass |

The owner can approve independently by replying with the ID and choice, for example
`D3: approve recommended`. A changed choice should include the replacement contract.

## D1 — Flat-price gallery vision and BYOK contract

**Observed:** public copy says every module is included at $40/month. The hosted beta
configuration currently funds one usage-bound live vision gallery per studio, capped at
150 images; that allowance is not tied to trial state. A studio xAI key takes precedence
for vision only, and exhausted hosted allowance falls back to deterministic mock
analysis. Album, content, and product provider funding use platform configuration and
are outside this decision. Storage visibility is a planning denominator, not a provider
bill.

### Options

1. **Unlimited Hestia-funded live gallery vision.** Simplest message, but there is no
   measured cost envelope supporting it. Do not choose this before paid usage and
   support costs exist.
2. **Flat core plus disclosed trial gallery vision and BYOK — recommended.** Keep the
   $40 price and every non-provider workflow. Include one live gallery analysis up to
   150 images during the trial; continued live gallery analysis uses the studio's own
   xAI key during beta.
3. **Hestia-metered AI add-on.** Avoids BYOK friction but adds usage billing, disputes,
   quota semantics, and another money-state machine before the core launch is proven.

**Recommended public contract:**

> Hestia is $40/month for the complete studio workflow. Your trial includes one live
> gallery analysis of up to 150 images. Continued live gallery analysis can use your
> studio's own xAI key during beta; non-provider workflows remain included.

**Smallest approval requested:** approve that sentence and confirm the allowance is a
trial benefit, not a recurring monthly allowance. The current usage-only allowance
must gain explicit trial-state enforcement before that sentence is published.

**Approval unlocks:** one copy/configuration slice aligning landing, pricing, signup,
billing, account state, trial-state enforcement, launch docs, and tests. It does not
decide payment or print commissions, package album/content/product provider funding,
or unlock metered billing, storage quotas, an annual plan, or paid provider
benchmarking.

**Acceptance:** the user sees whether a gallery analysis will be live before
submitting; only an eligible trial can consume the one hosted allowance; allowance
exhaustion never masquerades as live analysis; the studio vision key remains
tenant-scoped and never appears in logs/exports; public pages make no unlimited-AI or
universally-cheapest claim; usage remains observable without creating an invoice.

## D2 — Stripe client-funds and settlement contract

**Observed:** studio subscriptions and client invoice payments use the same platform
Stripe secret. Client Checkout has no connected-account routing, stored Checkout-attempt
binding, or Stripe idempotency key. The webhook's replay guard prevents duplicate local
fulfillment after one invoice is paid, but it does not prevent a second external charge
or prove that account, Session, paid status, amount, currency, livemode, metadata, and
invoice state all match.

Stripe's SaaS guidance describes direct payments where the connected account is the
merchant of record, and direct charges are reported on that connected account:
[SaaS direct payments](https://docs.stripe.com/connect/saas/tasks/accept-payment).
Connect webhooks identify the connected account and production endpoints can receive
both live and test events, so `account` and `livemode` must be checked:
[Connect webhooks](https://docs.stripe.com/connect/webhooks). Checkout creation should
also use a stable retry key:
[idempotent requests](https://docs.stripe.com/api/idempotent_requests).

### Options

1. **Platform-account charges.** Hestia receives client funds and owns payout,
   reconciliation, refund, dispute, tax, and merchant-of-record implications. That role
   is not covered by current $40 product copy, requires separate fee/legal terms, and may
   conflict with no-commission positioning. It is not recommended.
2. **Stripe Connect direct charges — recommended.** Each studio connects Stripe and
   receives its client's card payment directly. Hestia charges no application fee.
3. **No in-app live client payments for beta.** Keep invoice records but require the
   studio to collect and record payment elsewhere. This is the safe interim state until
   option 2 is implemented.

**Recommended exact contract:**

> Client invoices use card-only Stripe Connect direct charges to each studio's
> connected account, with no Hestia application fee. Hestia settles only a stored,
> Hestia-created Checkout attempt whose account, Session, mode, paid status, amount,
> currency, livemode, metadata, and invoice state all match. The first valid payment
> wins; duplicate or void-race payments become manual-refund incidents and never
> trigger fulfillment twice.

**Smallest approval requested:** approve the target contract and hold live client
invoice Checkout until it lands. Studio subscription billing remains a separate
platform charge.

**Approval unlocks:** a branch design for connected-account onboarding, a
payment-attempt ledger, idempotent Checkout creation, Connect webhook routing, incident
visibility, and card-only settlement. Because that slice changes money and schema, its
migration and merge return for explicit approval; this decision alone does not deploy it.

**Acceptance:** no attempt means no settlement; mismatched account, Session, mode,
status, amount, currency, livemode, or metadata fails closed; void invoices cannot
become paid; duplicate charges are visible and do not issue gift cards, orders, emails,
or lab jobs; no `application_fee_amount` is sent; tests cover retries, out-of-order
events, connected-account isolation, and manual-refund incidents.

## D3 — Media and gallery-PIN authorization contract

**Observed in an isolated probe:** a PIN-locked gallery page enforced its PIN, while its
thumbnail and original `/media/{token}` URLs both returned `200` without the PIN.
Delivery expiry and token rotation revoked `/d/...` but left `/media/...` usable.
Twenty wrong PIN submissions never returned `429`; the unlock cookie contained the
plaintext PIN, was site-wide, and lacked `Secure` under HTTPS settings. Local thumbnail
query removal exposed the original; S3 rendering instead emits one-hour presigned URLs
that do not have same-origin revocation parity.

### Options

1. **Independent permanent image capabilities.** Document that each published image
   token grants preview and original independently of PIN and delivery. Lowest change,
   but it makes delivery expiry and opt-in downloads misleading.
2. **Surface-inherited authorization — recommended.** Anonymous `/media/{token}` grants
   nothing. Proofing, delivery, offer, and album review each use scoped media routes and
   inherit that surface's current authorization.
3. **One unified client-room credential.** Make delivery token and optional PIN govern
   every client action. Cleaner eventually, but it breaks more current URLs and email/
   portal assumptions than option 2.

**Recommended contract:** proofing requires published + visible + signed PIN unlock
when a PIN is configured and serves derivatives only; delivery uses the current
unexpired delivery token and may serve originals; offer and album review are
preview-only; owner media remains
session-and-tenant scoped; hiding a frame revokes it everywhere; browser media remains
same-origin for local and S3. Unauthorized resources return `404`; expired delivery
returns `410` on every delivery route.

PINs become 4–12 ASCII digits stored as a keyed digest. Existing plaintext rows lazily
upgrade after one successful unlock. The signed cookie contains gallery ID, PIN
fingerprint, and expiry—never the PIN—and is `HttpOnly`, `SameSite=Lax`, `Secure`
on HTTPS, 24-hour, and gallery-path scoped. Five failures per 15 minutes per resolved
IP+gallery return `429` with `Retry-After`.

**Smallest approval requested:** approve option 2 and accept that old anonymous raw
`/media/...` tabs will fail closed after rollout.

**Approval unlocks:** a security-reviewed, no-schema route/PIN compatibility slice.
It does not authorize a later media-epoch schema, CDN-public objects, or widening CSP.

**Acceptance:** locked pages emit no usable media URL before unlock; suffix changes
never turn previews into originals; token rotation/expiry covers page, preview,
original, and ZIP; signed unlock rejects tampering, another gallery, expiry, and PIN
change; local and fake-S3 backends pass the same matrix; S3 HTML exposes only same-origin
URLs; logs contain no media credential or PIN.

## D4 — Migration-0065 history and checksum policy

**Observed:** the ledger records version/name/time, not source hashes. Original 0065
(commit `a3331e344d6eb86bc93cad5783bf850169bc3f08`, SHA-256 `f27e9d51...`)
created a `NOT NULL DEFAULT ''` column plus full unique index. Current 0065 (SHA-256
`81b33700...`) creates a nullable column plus partial unique index. Both can carry the
same ledger row. Thirty-seven migration files contain `ALTER TABLE`, and
`executescript` can leave committed DDL before the ledger insert.

The offline audit inventories every packaged migration against a committed manifest,
reports ledger gaps/name drift, recognizes exact current/original/pre/partial 0065
shapes, and states explicitly that schema signatures are evidence—not database checksum
attestation. It refuses every SQLite journal sidecar, opens only an isolated snapshot
with `mode=ro&immutable=1`, pins one read transaction, and verifies the database hash
and metadata are unchanged. SQLite documents why WAL readers otherwise need existing
sidecars, directory write access, or `immutable`:
[WAL read-only behavior](https://www.sqlite.org/wal.html#readonly).
A live source must first be copied with SQLite's
[Online Backup API](https://sqlite.org/backup.html).

### Options

1. **Support both recognized shapes for v1 — recommended.** Audit isolated snapshots of
   every real DB. Prove application compatibility against exact current and original
   fixtures. Fresh installs keep the current shape; historical installs stay unchanged.
2. **Normalize with a new forward migration.** Rebuild historical tables to the current
   shape only after rehearsal against copied real data. This has more live-data risk and
   needs a demonstrated behavioral benefit.
3. **Declare fresh-only launch.** Valid only if the owner proves no retained staging,
   beta, or production database can contain the original shape.

**Smallest approval requested:** choose the compatibility policy after running the
read-only audit on restored or SQLite online-backup snapshots of every retained
database. Recommended: approve option 1 and prohibit any edit to historical SQL.

**Approval unlocks:** compatibility fixtures and an explicit supported-state policy.
It does not authorize normalization. Any forward migration still requires a timestamped
backup, copy rehearsal, rollback procedure, reviewed SQL, and separate approval.

**Acceptance:** the manifest exactly covers packaged SQL; mutation fails CI; current,
historical, pre-0065, partial-DDL, gap, unknown-version, malformed-ledger, and name-drift
fixtures are distinct; diagnostic bytes, mtime, and sidecars remain unchanged; no
readiness gate treats a schema signature as a cryptographic record of applied SQL.

## D5 — Off-site durability evidence

**Observed:** daily SQLite backups are WAL-safe and restore-drilled.
`offsite-sync.sh` uses non-deleting `rclone copy` for DB backups and, with local
storage, media. It may still overwrite an existing object at the same path, so immutable
history requires destination-side versioning, object lock, or equivalent retention.
Preflight currently passes when a remote name or free-text durability acknowledgment
exists; it does not prove the sync ran, the newest DB reached the remote, or gallery
media is recoverable. An S3/R2 media backend is off-box, but its browser authorization
parity is held by D3.

### Options

1. **Configuration acknowledgment only.** Current behavior; cheap but proves intent,
   not recoverability.
2. **Local beta serving plus verified off-site copy — recommended.** Keep local media
   until D3 closes S3 parity. Use a destination with provider-side versioning, object
   lock, or equivalent immutable same-path retention. After each successful DB+media
   copy, verify the newest DB exists remotely and write an atomic, non-secret local
   receipt. Preflight checks that receipt's scope and freshness.
3. **Serve from private object storage now.** Strong off-box media durability, but hold
   until same-origin authorization/revocation and restore/export behavior are proven.

**Smallest approval requested:** choose the off-site provider/path and retention policy,
then approve option 2, including a maximum 26-hour receipt age. A generic
`HESTIA_MEDIA_DURABILITY_ACK` should not be a permanent hosted launch pass.

**Approval unlocks:** receipt generation, stale/missing/mismatched receipt failures,
remote-verification tests, and an operator drill. Credential creation and remote bucket
changes remain human actions.

**Acceptance:** copy never deletes remote objects; destination versioning/object lock
or equivalent retention protects prior bytes when a same-path object is replaced;
success is recorded only after DB and required media commands succeed and the newest DB
is verified remotely; the receipt contains no credentials and identifies storage mode,
destination fingerprint, newest DB artifact, and completion time; a stale receipt
blocks preflight; quarterly recovery starts from a real remote artifact and verifies a
known gallery's media, not only SQLite integrity.

## Next queue after D1–D5

Still held: public custom-domain/Caddy edge activation, subscription terminal-state
ordering, fulfillment provider/retry/shipping semantics, timezone/calendar schema,
repository rulesets, paid live-AI benchmark, runtime SQLite WAL-reset patch evidence,
and license/release truth. None should be silently absorbed into an approved D1–D5
slice.

The current environment template and runbooks are procedural holds, not a server-side
authorization boundary. Preflight, Admin Launch sharing, core invite helpers/redemption,
signup, client Checkout, the invoice branch of the shared Stripe webhook, and anonymous
media can still be reached without evidence that D1-D5 are closed. Before any public
release candidate, approve a committed, non-environment hosted hold across those
surfaces; replacing that hold requires reviewed, evidence-specific predicates and tests.
This changes authentication and payment behavior and therefore requires separate human
approval before implementation.

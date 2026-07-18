# Support playbook

> **Pre-launch draft.** Do not use live client-payment or media-privacy assurances until
> D2/D3 and the complete public release gate are closed.

The founder's first-response kit for the beta cohort. Every answer below is grounded
in a real product flow — nothing here promises a feature that doesn't exist. Reply
copy is ready to personalize and send.

## How to answer (the tone in three lines)

- **Fast beats polished.** A two-sentence reply in an hour beats a perfect one
  tomorrow. You're a photographer's colleague, not a ticketing system.
- **Answer, then teach.** Fix their problem in the first sentence; the second sentence
  shows them where the button lives so next time they don't need you.
- **Log the pattern.** Any question asked twice becomes a docs line, an empty-state
  hint, or a fix. The support inbox is the product backlog.

## The questions you'll actually get

### 1. "I never got my verification email"

Usual cause: spam folder or an SMTP hiccup. **Recovery is self-serve** — completing a
password reset proves the same mailbox ownership, so it also verifies the account.

> Check your spam folder for "Verify your email" first — but the fastest fix: go to
> **Sign in → Forgot password**, enter your email, and set a new password. That
> confirms your address and activates the studio in one step.

Founder side: if several people report this, mail isn't leaving the box — check the
email seam on `/admin/system` and SPF/DKIM per `docs/deploy-wiring.md`.

### 2. "I can't log in"

Three causes, one answer: unverified (see #1), forgotten password, or a different
signup email. All three resolve through **`/forgot`** — the reset email tells them
which address is actually registered (no mail = wrong address).

### 3. "How do my clients see their photos?"

> Open the gallery and copy its **delivery link** — that's the client's private URL
> (favorites, proofing, downloads, and any print offer live behind it). Send it like
> you'd hand over a key: the link *is* the access.

If they use the client portal: the action room gathers galleries, contracts, invoices,
and forms behind one client link.

### 4. "My client lost the link" / "I think the link leaked"

Lost: re-copy it from the gallery page and resend — links don't expire on their own.
Leaked: rotate the gallery/portal link, then treat it as a security incident. Rotation
does not currently revoke separately issued per-image media tokens or an already issued
S3 presigned URL; keep affected media private and follow D3 rather than promising instant
revocation.

### 5. "A client's payment didn't go through"

Live client-invoice payment is held by D2. Do not send or retry a customer payment link.
The current platform-account Checkout/webhook path lacks the approved Connect attempt
binding and idempotency evidence; use only the private Stripe test-mode subscription
rehearsal described in `docs/deploy-wiring.md`.

### 6. "My own card failed" / "am I about to lose access?"

No — there's a grace period. Hestia emails them automatically (every 4 days) until
the card is fixed, and access continues meanwhile.

> No rush — your studio keeps working. Update your card at **Settings → Billing**
> whenever you have a minute and everything continues as normal.

### 7. "How do I cancel? What happens to my data?"

Self-serve at **Settings → Billing → Cancel**. Their data stays intact — the studio
downgrades rather than deletes, so coming back later is painless. Refund handling and
any beta promise remain an owner/legal decision; do not promise a policy that has not
been recorded and implemented.

### 8. "Can I get my data out?"

Yes, honestly:

- **Clients** → CRM → **Export CSV** (tag-filterable; the same page imports CSVs from
  other tools, so migration runs both directions).
- **Money** → Finances → income and expense **CSV exports**.
- **Photos** — originals stay theirs; galleries download per gallery. No bulk
  all-galleries archive yet: for a full-studio exit, offer to pull their originals
  from storage yourself. Don't pretend a button exists.

### 9. "Is my clients' data private?" / a security report

Short answer for users: tenant data is isolated, client links are unguessable capability
tokens, and private pages are marked against search indexing. Do not claim a gallery PIN
revokes or gates raw per-image media URLs until D3 lands. Full posture:
`docs/security.md`. **If someone reports a vulnerability**, thank them same-day and
follow the responsible-disclosure section there — a good-faith reporter is a friend.

## Escalation ladder

1. **You, within hours** — everything above.
2. **The incident table** in `docs/operations.md` — anything smelling of outage,
   failed backups, or webhook silence.
3. **`docs/security.md` incident response** — anything touching data exposure or
   credentials. Rotate first, apologize second.

When you break something: say so, plainly, to the studios affected. Beta users forgive
bugs; they don't forgive silence.

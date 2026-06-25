# Hestia — Phase 1 (stub)

Phase 0 proved the magic moment in one app. Phase 1 makes it a real, sellable SaaS.

## 1. Live revenue — Stripe checkout on offers

- Stripe Customer per studio (`tenants.stripe_customer_id`).
- Client offer page → Stripe Checkout Session per bundle (`mode=payment`),
  webhook-confirmed orders. Build on `hestia/billing.py` (today a scaffold).
- Studio subscription plans (Beta / Studio / Studio Pro) with gallery limits.
- Test-mode by default; a `HESTIA_STRIPE_LIVE_ENABLED` gate for live keys.

## 2. Cloud storage — S3 / R2

- Implement the `s3` branch of `hestia/storage.build_storage` against the existing
  `Storage` interface (`put/open/exists/delete/public_path`).
- `public_path` returns a signed URL instead of a `/media/...` route.
- Migration: copy local blobs to the bucket; keep keys tenant-scoped.

## 3. Album-design module (essence of Mnemosyne)

- New in-process `albums.py`: draft lay-flat spreads from a gallery + vision heroes,
  "model judges, code validates" (an LLM proposes order/grouping; code guarantees
  every photo is placed once). Surface an album bundle that links to the draft.
- Gate by shoot type (`features.album_offer`, already wired).

## 4. Public signup

- Flip `HESTIA_SIGNUP_ENABLED=true` behind email verification + invite codes.
- Self-serve studio creation (today admin-only), rate limiting, abuse controls.

## 5. Live vision hardening

- Default `HESTIA_VISION_BACKEND=xai`; per-studio cost caps + metering; async job
  queue for very large galleries (mock/sync is fine for Phase 0 sizes).
- Cache analyses; only re-analyze new/changed frames.

## 6. Adjacent product lines (separate, not folded in)

The research found two siblings that target different customers — marketing copy
(Dionysus, F&B) and e-commerce packshots (Aphrodite). These are **separate
products** on the same framework, not Hestia modules. Revisit only if a studio
customer pulls them in.

## Order of operations

1. Stripe checkout on offers (the revenue unlock).
2. S3/R2 storage (the multi-host unlock).
3. Album module (the wedding/event upsell).
4. Public signup (the growth unlock).

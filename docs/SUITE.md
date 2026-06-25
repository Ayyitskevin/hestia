# Suite integration reference (for AI agents)

Hestia calls these services over HTTP. Read each repo's routes before adding client methods.

## Argus (`HESTIA_ARGUS_URL`, default `:8010`)

| Concern | Notes |
|---------|--------|
| Role | Vision: keywords, keeper/hero scores, shot types |
| Health | `GET /healthz` |
| Analyze | Folder or job queue — see `argus/app/routes/` |
| Auth | Optional bearer `ARGUS_API_TOKEN` |

Mnemosyne and Plutus already consume Argus; Hestia should reuse run IDs when present.

## Plutus (`HESTIA_PLUTUS_URL`, default `:8031` SaaS)

| Route | Purpose |
|-------|---------|
| `POST /recommend/mise-gallery` | Mise publish hook (legacy) |
| `POST /webhooks/mise/gallery-published` | SaaS publish webhook |
| `POST /integrations/offer` | Mint share/offer link (canonical for automation) |
| `POST /storefront/share-links` | Same as offer (tenant bearer) |
| `GET /healthz` | Dependency checks |

Auth: `Authorization: Bearer plutus_tk_<tenant>_<token>`

Reference: `plutus/scripts/dogfood-suite-loop.sh`

## Mnemosyne (`HESTIA_MNEMOSYNE_URL`, default `:8000`)

| Concern | Notes |
|---------|--------|
| Import | `GET/POST` Mise gallery import — `mnemosyne/src/mnemosyne/mise_import.py` |
| Plutus link | `mnemosyne/src/mnemosyne/plutus_api.py` — cross-sell from offer |
| When | Shoot types `wedding`, `event` only in Phase 0 |

## Dionysus (`HESTIA_DIONYSUS_URL`, default `:8450`)

| Route | Purpose |
|-------|---------|
| `GET /api/mise/organizations/{slug}/latest-pack` | Read approved campaign pack |
| `POST /api/mise/organizations/{slug}/argus-pack` | Draft from Argus run |

Auth: `Authorization: Bearer <DIONYSUS_MISE_IMPORT_TOKEN>`

When: shoot types `commercial`, `food` only in Phase 0.

## Mise (`HESTIA_MISE_URL`, default `flow:8400`)

| Concern | Notes |
|---------|--------|
| Role | Gallery source, CRM, site — **Phase 2 deep integration** |
| Plutus hook | `mise/app/plutus_recommend.py` — POST on publish |
| Argus hook | `mise/app/argus_analyze.py` — vision on publish |
| Phase 0 | Hestia triggers pipeline with `mise_gallery_id`; BYO Mise URL per tenant |

Mise is **single-tenant** today on the operator fleet. Do not assume multi-tenant Mise in Phase 0.

## Shared conventions

- Propagate `tenant_id` (Hestia UUID/slug) in pipeline metadata where downstream supports it.
- Pipeline steps must be **idempotent** — safe to retry after partial failure.
- If a optional service is down, complete the core Argus → Plutus path and mark optional steps `skipped`.
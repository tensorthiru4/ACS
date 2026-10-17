# ACS Agent-Readiness Auditor (Playwright, Gemini Flash, A2A)

Simulates AI-shopper journeys against a merchant storefront and scores how usable it is for agents.

```
Orchestrator ─A2A audit_store (returnImmediately)─► task SUBMITTED (Firestore task store)
                                                    └─► Cloud Tasks ─► POST /internal/audits/{id}/run
                                                             ├─ Playwright journeys ─► storefront (UCP endpoints)
                                                             ├─ deterministic checks ─► score
                                                             ├─ Gemini Flash ─► tailored fixes (fallback: built-in)
                                                             └─ BigQuery load jobs ─► acs.readiness_scores, acs.audit_fixes
Orchestrator ─A2A GetTask─► WORKING → COMPLETED (DataPart: audit_id, score, journeys, fixes)
```

Contract: [`docs/contracts/README.md#1`](../../docs/contracts/README.md#1-agent-readiness-auditor--a2a-v10-json-rpc).

**Why Cloud Tasks:** Cloud Run only allocates CPU while a request is in flight. If the audit
ran as background work after the A2A reply, it would be throttled to nearly zero. Each audit
therefore runs as its own OIDC-authenticated request. The task name is derived from the A2A task id,
so enqueueing is idempotent, and a redelivered run for a finished task is a no-op.

## Journeys and checks

| Journey | Checks (weight) |
|---|---|
| discovery | UCP profile at `/.well-known/ucp` (3), services declared (2), robots.txt lets agents in (2), sitemap (1), store JSON-LD (2), title/description (1) |
| product_detail | product page reachable (2), Product JSON-LD (3), complete offer (3), sku/gtin/mpn (1), labelled controls (1) |
| cart | add-to-cart button (3), add-to-cart works (2), UCP cart/checkout capability (2) |
| checkout | UCP checkout capability (3), checkout reachable (2), no CAPTCHA wall (2), payment handlers (1) |
| returns | returns page (2), MerchantReturnPolicy JSON-LD (3), UCP refund/return capability (1) |

Scores come only from these checks. Gemini only rewords the fix text for checks that failed.

## Network safety

The URL is https only, on the default port, with no credentials and no IP literals. The host is
resolved once, every address must be public, and Chromium is pinned to that address with
`--host-resolver-rules`, which defeats DNS rebinding. Every request goes through a route guard that
aborts other hosts, non-https URLs, images, media and fonts. Forms are never filled in. Sitemaps with
DTDs are refused.

## Run locally

```bash
uv sync && uv run playwright install --only-shell chromium
uv run pytest
uv run python -m acs_auditor.main     # ACS_ENV=local runs audits in-process
```

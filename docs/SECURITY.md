# Security model

These are the defense-in-depth layers, from the edge inward. Each layer assumes the one before it
may fail.

## 1. Edge: Firebase Hosting and API Gateway
* **Firebase Hosting** serves the Merchant Console with HSTS, a strict CSP, `frame-ancestors 'none'`
  and `nosniff` (`firebase/firebase.json`).
* **API Gateway** (`infra/terraform/openapi/gateway.yaml.tftpl`) checks each route before any
  request reaches Cloud Run:
  * Console routes require a Firebase ID token, validated for issuer, audience and signature.
  * Machine routes require a gateway API key restricted to this gateway. Each channel gets its own key.
  * Only discovery documents (UCP profile, A2A agent card, JSON schema) are public.
* The gateway calls Cloud Run as its own service account. The caller's credential moves to
  `X-Forwarded-Authorization`.

## 2. Network and identity: Cloud Run IAM
* No service has an `allUsers` invoker binding. The orchestrator can be invoked only by the gateway
  SA. Each downstream agent can be invoked only by the services that have a diagram edge to it
  (`local.agent_invokers`).
* Service-to-service calls use Google-signed ID tokens minted from the metadata server
  (`clients/service_auth.py`). There are no shared secrets and no JSON keys.
* Each workload has its own service account and only the roles its edges need (`iam.tf`). For
  example, the orchestrator can publish only to `po-intake-events`, and the messy-order agent can
  create objects but not delete them.

## 3. Application authentication (`security/auth.py`)
* **Firebase tokens are re-verified** in the app: signature, expiry, revocation
  (`check_revoked`), required `merchant_id` and `role` custom claims, and anonymous sign-in is
  rejected. The app never trusts the gateway's decoded user-info header.
* **ACS client credentials** for shopper agents and B2B partners use the form
  `acsk_<id>.<secret>`. Only `HMAC-SHA256(pepper, secret)` is stored, the pepper lives in Secret
  Manager, comparison is constant-time, and unknown ids take the same path as known ones.
* **UCP identity binding:** the `UCP-Agent` profile must equal the profile registered for the
  credential. Otherwise the request fails with 401, as UCP requires for conflicting identities.
* **Gmail push** carries a Google OIDC token checked for audience, verified e-mail and the
  pinned push service account.
* The local dev impersonation header is refused unless `ACS_ENV=local`. It is also always refused
  when `K_SERVICE` is set (on Cloud Run).

## 4. Authorization (`security/principals.py`)
* There is one deterministic capability table. Merchant roles are `viewer < analyst < admin`.
  Machine principals get fixed capabilities.
* Tenant isolation: every operation uses `principal.merchant_id` from the verified credential.
  Request bodies forbid unknown fields, so a `merchant_id` cannot be injected. A2A task ids, policy
  drafts, intake records and ADK sessions are all scoped by tenant and owner.

## 5. LLM and agent guard rails
* **The model holds no authority.** Tools re-check capabilities in a `before_tool_callback`
  (allow-list, RBAC, and a budget of 8 tool calls per turn). Tenant ids are never tool arguments.
* **Refund outcomes come only from the CEL rule engine.** Any engine error fails safe to
  `ESCALATE` and is never approved.
* **Rule deployment cannot be reached from the model.** It needs an admin calling
  `POST /v1/policies/{id}/deploy` after reviewing the draft.
* Input screening (`security/guardrails.py`) applies NFKC normalization, strips invisible and bidi
  characters, enforces length limits and blocks prompt-injection patterns before the model runs.
* Downstream and web content returned to the model is wrapped as `untrusted_data`.
* An `after_model_callback` redacts secrets and PII patterns from model output.
* The A2A executor never sees credential headers. The shopper-facing agent has a single tool.

## 6. Input handling
* Bodies are capped at 9 MiB (by declared length and while streaming) and POs at 8 MiB, which fits
  the Pub/Sub 10 MB limit.
* File types are detected from magic bytes against an allow-list (PDF and common audio formats).
  PDFs with JavaScript, Launch, embedded files, XFA or encryption are rejected. Filenames are sanitized.
* **SSRF:** audit URLs must be https on the default port with no credentials and no IP literals.
  The host must be one of the merchant's registered storefront domains, and DNS must resolve only
  to global addresses. The A2A client pins the configured origin, so a malicious agent card cannot
  redirect traffic or tokens.
* E-mail POs must come from allow-listed sender domains and pass DMARC (or both SPF and DKIM).
* Every write is idempotent: refund decisions use `Idempotency-Key`, and POs are deduplicated by
  tenant plus SHA-256.

## 7. Data protection
* Firestore and Storage rules deny all client access; only service accounts reach data.
* The GCS bucket enforces public-access prevention and uniform bucket-level access, and lifecycle
  deletes raw POs after 90 days.
* Firestore TTL purges ADK sessions and events after 7 days.
* Secrets are bound at deploy time from Secret Manager and never logged.

## 8. Observability and response
* Logs are structured JSON with request id and trace correlation. A recursive redaction filter
  removes JWTs, keys, card numbers and e-mail addresses.
* `audit=true` events (auth failures, tool calls and denials, refunds, deployments, intake) flow
  through a log sink into BigQuery `acs` for investigation.
* A per-principal token-bucket rate limit returns 429. Max instances are capped, and a budget alert
  fires at $1.

## 9. Supply chain
* `uv.lock` pins dependencies.
* The image uses a multi-stage slim build, runs as non-root uid 10001 and contains no build tools.
* Cloud Build runs tests and a Trivy scan, which fails on fixable HIGH/CRITICAL findings, and
  deploys by **digest**.
* Artifact Registry tags are immutable. GitHub deploys use keyless Workload Identity Federation
  behind a protected `production` environment.

## Known limitations and next hardening steps
* The rate limiter is per instance and approximate. For strict quotas, add API Gateway quotas or a
  Firestore counter.
* The A2A inbound task store is in memory. Because requests are blocking this is fine, but it
  should move to Firestore before streaming or async tasks are enabled.
* The PDF active-content check is a byte-level heuristic, so compressed object streams can hide
  markers. The messy-order agent should parse PDFs in a sandbox.
* Consider Cloud Armor in front of the gateway when traffic grows. It is not free tier.

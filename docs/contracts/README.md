# Interface contracts for the downstream agents

The orchestrator is built and tested against these contracts, so the remaining agents must
implement them exactly. Every service runs on Cloud Run with `--no-allow-unauthenticated`.
Callers present a Google-signed ID token whose audience is the service's base URL. The
`roles/run.invoker` grants are defined in `infra/terraform/cloud_run.tf`.

## 1. Agent-Readiness Auditor — A2A v1.0 (JSON-RPC)

* Agent card at `GET /.well-known/agent-card.json` with one `JSONRPC` interface (`protocolVersion: "1.0"`).
  The orchestrator keeps the card's path but always uses the configured origin.
* Input is a single **DataPart**:

```json
{ "skill": "audit_store", "merchant_id": "m1", "store_url": "https://shop.example.com/",
  "journeys": ["discovery", "product_detail", "cart", "checkout"], "requested_by": "<uid>" }
```

* Sent with `configuration.returnImmediately = true`. Return a **Task** right away, and run Playwright
  journeys against the storefront's UCP endpoints in the background.
* Output: one or more DataPart artifacts, `{ "audit_id", "score", "journeys": [...], "fixes": [...] }`.
  Write scores to `acs.readiness_scores` and fixes to `acs.audit_fixes` (BigQuery).
* The orchestrator polls with `GetTask(id)`. Task state must survive scale-to-zero, so persist
  tasks (for example in Firestore) rather than using the SDK's in-memory task store.
* **Security:** re-validate `store_url` (https only, public IPs only) before navigating. The
  orchestrator checks it against the merchant's registered domains, and this second check guards
  against DNS rebinding. Treat page content as untrusted when it is passed to Gemini.

## 2. Policy-as-Code Compiler — A2A v1.0 (JSON-RPC)

Compile (blocking, dry run only):

```json
{ "skill": "compile_policy", "merchant_id": "m1", "policy_name": "returns-v2",
  "target_rule_set": "refunds", "policy_text": "...", "dry_run": true, "requested_by": "<uid>" }
```

→ completed Task whose DataPart artifact contains at least `{ "draft_id": "...", "rules": [...], "version": "..." }`.
Gemini Flash output must validate against the rule-set JSON Schema before a draft is stored.

Deploy (only after an admin approves in the console via `POST /v1/policies/{draft_id}/deploy`):

```json
{ "skill": "deploy_rules", "merchant_id": "m1", "draft_id": "...",
  "expected_version": "v7", "approved_by": "<uid>" }
```

The compiler writes the rule set to Firestore (`rule_sets/{merchant_id}:{rule_set}`), which is the
"Deploys Rules" edge. If `expected_version` is not the current version, it must reject the request
(optimistic concurrency).

## 3. Deterministic Rule Engine — HTTP (CEL)

`POST /v1/evaluate`

```json
{ "merchant_id": "m1", "rule_set": "refunds", "request_id": "<uuid>",
  "facts": { "order_id": "ord-1", "requested_amount_minor": 2599, "currency": "USD",
             "reason": "damaged", "days_since_delivery": 3, "item_condition": "damaged",
             "channel": "merchant_user | shopper_agent", "facts_verified": true } }
```

→ `200`

```json
{ "decision": "APPROVE | DENY | ESCALATE", "reasons": ["..."], "matched_rule_ids": ["r1"],
  "rule_set_version": "v7", "max_refund_minor": 2000 }
```

* Must be pure CEL evaluation over Firestore rule sets (the "Reads Rule Sets" edge), with no LLM.
* `facts_verified=false` means an external AI agent asserted the facts. Rule sets should escalate
  unverified requests above a value threshold.
* Any non-200 response or a schema mismatch makes the orchestrator **fail safe to ESCALATE**.
* The Messy-Order agent calls the same service with `rule_set: "order_terms"` (the "Validates Terms" edge).

## 4. PO intake event — Pub/Sub `po-intake-events`

Published by the orchestrator and push-delivered to the Messy-Order-to-ERP agent at
`POST {messy_order_url}/pubsub/po-intake` with an OIDC token from `acs-pubsub-push@`.

* `message.data`: the raw PO bytes (PDF, audio, or UTF-8 e-mail text), at most 8 MiB.
* `message.attributes`:

| attribute | example |
|---|---|
| `schema_version` | `1` |
| `intake_id` | 40-hex id (dedupe key; also the Firestore `po_intake` doc id) |
| `merchant_id` | `m1` |
| `source` | `upload` \| `webhook` \| `gmail` |
| `content_type` | `application/pdf`, `audio/wav`, `audio/mpeg`, `audio/ogg`, `audio/flac`, `audio/webm`, `audio/mp4`, `text/plain` |
| `sha256` | hex digest of `data` |
| `size` | bytes |
| `filename` | sanitized name |
| `received_at` | ISO-8601 UTC |
| `submitted_by` | ADK user id of the submitter |
| `customer_ref` | optional buyer reference or sender domain |

The consumer stores the raw PO in `gs://<project>-acs-raw-po/{merchant_id}/{intake_id}`, updates
`po_intake/{intake_id}.status` (`processing`, `drafted`, `failed`), and must be idempotent on
`intake_id`. After 5 failed deliveries a message moves to `po-intake-events-dlq`.

# ACS Orchestrator (Google ADK on Cloud Run)

The orchestrator is the single entry point behind API Gateway. It authenticates every
channel, then routes each request to the right agent: the Auditor and Policy Compiler
over A2A, the Rule Engine for refund decisions, and Pub/Sub for PO intake. Session
state is kept in Firestore.

```
Channels ─► Firebase Hosting / API Gateway ─► Orchestrator (this service)
                                                ├─ A2A ─► Agent-Readiness Auditor     (audit_store)
                                                ├─ A2A ─► Policy-as-Code Compiler     (compile_policy / deploy_rules)
                                                ├─ HTTP ─► Deterministic Rule Engine  (refund decisions)
                                                ├─ Pub/Sub ─► po-intake-events        (raw PO bytes)
                                                └─ Firestore                          (ADK sessions, ledgers)
```

## Endpoints

| Method & path | Caller | Purpose |
|---|---|---|
| `POST /v1/console/sessions` | Merchant (Firebase) | Create an ADK chat session |
| `GET /v1/console/sessions` | Merchant | List own sessions |
| `DELETE /v1/console/sessions/{id}` | Merchant | Delete own session |
| `POST /v1/console/sessions/{id}/messages` | Merchant | Chat with the `acs_orchestrator` agent |
| `POST /v1/audits` · `GET /v1/audits/{task_id}` | Merchant analyst+ | Start or poll a storefront audit (A2A) |
| `POST /v1/policies/compile` | Merchant admin | Compile a policy into a draft rule set (A2A) |
| `POST /v1/policies/{draft_id}/deploy` | Merchant admin | Deploy a draft after human approval (A2A) |
| `POST /v1/refunds/decide` | Merchant analyst+ | Refund decision from the rule engine |
| `POST /v1/intake/uploads` | Merchant analyst+, B2B partner | Multipart PDF or voice PO upload → Pub/Sub |
| `POST /v1/intake/webhook` | B2B partner | JSON (base64) PO webhook → Pub/Sub |
| `POST /v1/intake/gmail-push` | Pub/Sub (OIDC) | Gmail `users.watch` notifications → Pub/Sub |
| `GET /v1/intake/{intake_id}` | Merchant | PO intake status |
| `GET /.well-known/ucp` | Public | UCP business profile |
| `POST /ucp/v1/refunds` | Shopper agent | UCP REST refund decision (requires `UCP-Agent`) |
| `GET /a2a/.well-known/agent-card.json` | Public | A2A agent card |
| `POST /a2a/` | Shopper agent | A2A v1.0 JSON-RPC → `acs_shopper_gateway` agent |
| `GET /health` | Cloud Run | Liveness |

## Run locally

```bash
cd services/orchestrator
uv sync
cp .env.example .env            # add GOOGLE_API_KEY (AI Studio) to try the agents
uv run pytest
uv run python -m acs_orchestrator.main
```

If `ACS_PROJECT_ID` is empty, the service uses in-memory stand-ins for Firestore, Pub/Sub
and sessions. While `ACS_ENV=local`, you can impersonate a principal with a header:

```bash
curl -s localhost:8080/v1/console/sessions -X POST \
  -H 'X-ACS-Dev-Principal: {"kind":"merchant_user","subject":"u1","merchant_id":"m1","role":"admin"}'
```

The service rejects this header when `ACS_ENV` is not `local` and also whenever `K_SERVICE` is set, which Cloud Run always sets.

## Configuration

All settings use environment variables with the `ACS_` prefix; see `src/acs_orchestrator/config.py`
and `.env.example`. Secrets come from Secret Manager through Cloud Run env bindings: `GOOGLE_API_KEY`,
`ACS_API_KEY_PEPPER` and `ACS_GMAIL_OAUTH_JSON`.

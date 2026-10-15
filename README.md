# ACS — Autonomous Commerce Suite

A multi-agent commerce system built to run on the **Google Cloud free tier**, implementing
[`architecture.pdf`](architecture.pdf).

```
Channels                         Front door                     Agents on Cloud Run (scale to zero, us-central1)
───────────────────────────      ──────────────────────────     ────────────────────────────────────────────────
AI Shopper Agents (UCP, A2A) ─┐                                 ┌─ A2A ─► Agent-Readiness Auditor ─► BigQuery (scores, fixes)
Merchant Console (Firebase)  ─┼─► Firebase Hosting + Auth   ─►  │                 └─ simulates journeys ─► Merchant storefront (UCP)
B2B Inbox (Gmail/PDF/voice)  ─┘   + API Gateway                 ├─ A2A ─► Policy-as-Code Compiler ─► deploys rules ─┐
                                        │                       │                                                    ▼
                                        ▼                       ├─ refund decisions ─────────────────► Deterministic Rule Engine (CEL) ◄─ reads rule sets ─ Firestore
                              ACS ORCHESTRATOR (ADK) ───────────┤
                                                                ├─ publishes PO ─► Pub/Sub ─push─► Messy-Order-to-ERP agent ─► ERP bridge (Workflows) ─► Odoo
                                                                └─ session state ─► Firestore            ├─ validates terms ─► Rule Engine
                                                                                                         ├─ vector SKU match ─► Firestore
                                                                                                         └─ stores raw PO ─► Cloud Storage
```

## Status

| Component | Status | Location |
|---|---|---|
| **ACS Orchestrator** (Google ADK on Cloud Run) | ✅ Implemented and tested | [`services/orchestrator`](services/orchestrator) |
| Infrastructure (all diagram resources, IAM, gateway) | ✅ Terraform, validated | [`infra/terraform`](infra/terraform) |
| Firebase Hosting / rules | ✅ | [`firebase`](firebase) |
| CI/CD (Cloud Build + GitHub Actions/WIF) | ✅ | [`cloudbuild`](cloudbuild), [`.github/workflows`](.github/workflows) |
| Agent-Readiness Auditor | ⏳ Contract defined | [`docs/contracts`](docs/contracts/README.md#1-agent-readiness-auditor--a2a-v10-json-rpc) |
| Policy-as-Code Compiler | ⏳ Contract defined | [`docs/contracts`](docs/contracts/README.md#2-policy-as-code-compiler--a2a-v10-json-rpc) |
| Deterministic Rule Engine | ⏳ Contract defined | [`docs/contracts`](docs/contracts/README.md#3-deterministic-rule-engine--http-cel) |
| Messy-Order-to-ERP Agent | ⏳ Event contract defined | [`docs/contracts`](docs/contracts/README.md#4-po-intake-event--pubsub-po-intake-events) |
| ERP Bridge + Safety Gate (Workflows) and Odoo | ⏳ SA + secret provisioned | — |
| Merchant Console SPA | ⏳ Hosting config ready | `console/` (to be added) |

Until a downstream agent is deployed, the orchestrator handles its absence safely. Audits and
policy calls return `502 <agent> is not configured`, and refund requests are answered with
`ESCALATE`.

## Docs

* [Security model](docs/SECURITY.md): the defense layers from edge to data.
* [Deployment runbook](docs/DEPLOY.md): free-tier budget table, bootstrap, secrets, tenants.
* [Agent contracts](docs/contracts/README.md): the A2A, HTTP and Pub/Sub interfaces the orchestrator uses.

## Quick start (local)

```bash
cd services/orchestrator
uv sync
uv run pytest            # 41 tests; Firestore emulator tests run when FIRESTORE_EMULATOR_HOST is set
uv run python -m acs_orchestrator.main
```

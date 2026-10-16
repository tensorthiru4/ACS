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
| **Agent-Readiness Auditor** (Playwright + Gemini Flash, A2A) | ✅ Implemented and tested | [`services/auditor`](services/auditor) |
| **Policy-as-Code Compiler** (Gemini Flash, JSON Schema, A2A) | ✅ Implemented and tested | [`services/policy_compiler`](services/policy_compiler) |
| **Deterministic Rule Engine** (CEL) | ✅ Implemented and tested | [`services/rule_engine`](services/rule_engine) |
| **Messy-Order-to-ERP Agent** (Gemini multimodal, vector SKU match) | ✅ Implemented and tested | [`services/messy_order`](services/messy_order) |
| **ERP Bridge + Safety Gate** (Cloud Workflows → Odoo XML-RPC) | ✅ Implemented (not yet executed against Odoo) | [`workflows/erp_bridge.yaml`](workflows/erp_bridge.yaml) |
| Shared agent library (auth, Gemini, A2A task store, CEL core) | ✅ | [`libs/acs_common`](libs/acs_common) |
| Infrastructure (all diagram resources, IAM, gateway, Cloud Tasks, Workflows) | ✅ Terraform, validated | [`infra/terraform`](infra/terraform) |
| Firebase Hosting / rules | ✅ | [`firebase`](firebase) |
| CI/CD (Cloud Build + GitHub Actions/WIF) | ✅ | [`cloudbuild`](cloudbuild), [`.github/workflows`](.github/workflows) |
| Odoo Community (self-hosted) | ⏳ Bring your own; credentials go in the `odoo-credentials` secret | — |
| Merchant Console SPA | ⏳ Hosting config ready; APIs exist | `console/` (to be added) |

Downstream agents are optional at deploy time. While one is missing, the orchestrator degrades
safely: audit and policy calls return `502 <agent> is not configured`, refund requests are
answered with `ESCALATE`, and draft-order review returns 502.

## Docs

* [Security model](docs/SECURITY.md): the defense layers from edge to data.
* [Deployment runbook](docs/DEPLOY.md): free-tier budget table, bootstrap, secrets, tenants.
* [Agent contracts](docs/contracts/README.md): the A2A, HTTP and Pub/Sub interfaces the orchestrator uses.

## Quick start (local)

```bash
cd services/orchestrator
uv sync
uv run pytest            # orchestrator tests; Firestore emulator tests run when FIRESTORE_EMULATOR_HOST is set
uv run python -m acs_orchestrator.main
```

Each agent is a separate uv project that depends on `libs/acs_common`:

```bash
for p in libs/acs_common services/rule_engine services/policy_compiler services/auditor services/messy_order; do
  (cd $p && uv sync --all-extras && uv run pytest -q)
done
```

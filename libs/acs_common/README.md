# acs-common

Shared code for the ACS downstream agents (rule engine, policy compiler, auditor and
messy-order agent). The orchestrator predates this package and keeps its own copies.

| Module | Purpose | Extra |
|---|---|---|
| `logging_setup` | JSON logs for Cloud Logging, `audit()` events routed to BigQuery | — |
| `redaction` | Redacts secrets and PII before logging | — |
| `service_auth` | Outbound ID-token client, inbound caller allow-list (defense in depth) | `web` |
| `gemini` | Gemini client with transient-error retries and schema-constrained JSON output | `gemini` |
| `a2a_store` | Firestore `TaskStore` so A2A tasks survive scale-to-zero | `a2a` |
| `rules` | Rule-set schema, fact schemas, CEL validation and deterministic evaluation | `rules` |

```bash
uv sync --all-extras
uv run pytest
```

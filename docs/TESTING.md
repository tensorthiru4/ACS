# Testing ACS

Three layers, from fastest to most realistic.

## 1. Unit and contract tests (no cloud, no cost)

Every project has its own suite. LLMs, Firestore, Pub/Sub, Cloud Tasks and the rule engine are
faked, but the agent-to-agent contracts run for real. A2A calls go through a real a2a-sdk client,
and push events use real envelopes.

```bash
for p in libs/acs_common services/rule_engine services/policy_compiler services/auditor services/messy_order services/orchestrator; do
  (cd $p && uv sync --all-extras && uv run pytest -q)
done
cd console && npm install && npm test        # API error handling, formatting, input patterns
```

## 2. Merchant Console against the live system

```bash
cd console
cp .env.example .env.local      # Firebase web config + gateway URL (see console/README.md)
npm run dev                     # http://localhost:5173
```

Sign in with a user that has `merchant_id` and `role` claims
(`acs-admin set-claims --uid … --merchant m1 --role admin`). Then work through these scenarios:

| Scenario | Where | Expected result |
|---|---|---|
| Refund with no policy deployed | Refunds: damaged, ₹2,900, 3 days | `ESCALATE`, "no rule matched", rule set "none deployed" |
| Unverified high-value refund | Ask the Assistant as a shopper agent, or call `/ucp/v1/refunds` | `ESCALATE` from the `sys_unverified_high_value` guard |
| Compile a policy | Policies → order_terms, e.g. "auto-approve known customers up to ₹2,00,000…" | Draft with rules, test table and assumptions. Live rules are unchanged |
| Deploy | Approve and deploy | `v1` is live within 30 s. Refunds and orders now cite the rule ids |
| Stale deploy | Deploy an older draft after a newer one | Rejected: "rule set is at v2, not v1" |
| PO intake | PO intake: upload `samples/wine-po-vine-and-dine.pdf`, customer `vineanddine.example` | Status `queued`, then `drafted` within about a minute |
| Matching | Draft orders: open the PO | Shiraz is an *exact* match (alias `SHZ-NSK-22`); Malbec, NZ Sauvignon Blanc and Champagne match by *vector*; whisky is *unmatched* |
| Human review | Correct or reject the whisky line, then Approve and post to Odoo | Status `approved` → `posting` → `posted`; a quotation appears in Odoo with origin `ACS:<intake id>` |
| Safety gate | Release an order above `erp.max_auto_post_minor` | Status `erp_blocked` with the reason. Nothing reaches Odoo |
| Store audit | Store audits: a registered storefront URL | `SUBMITTED` → `WORKING` → score with fixes. Rows land in BigQuery `acs.readiness_scores` |
| Tenancy and roles | Sign in as a `viewer` | No Policies or Refunds pages. Release returns 403 |

## 3. Watching the agents work

```bash
# Every decision is an audit event (also copied to BigQuery acs.* by the log sink)
gcloud logging read 'jsonPayload.audit=true' --freshness 1h --limit 20 \
  --format='value(resource.labels.service_name,jsonPayload.action,jsonPayload.outcome)'
# ERP bridge runs
gcloud workflows executions list acs-erp-bridge --location us-central1 --limit 5
```

Firestore collections worth opening in the console: `po_intake`, `draft_sales_orders`,
`rule_sets`, `policy_drafts` and `a2a_task_state`.

## Gemini quota

On the AI Studio free tier, **each model allows 20 requests a day**, and every agent shares
`gemini-3.8-flash`. One PO uses 1 call, a policy compile 1–2, a chat turn 1 or more, and an audit 1.
A 503 "high demand" from the model is transient. A 429 with `PerDay` in the message means wait for
the daily reset or enable billing on the key.

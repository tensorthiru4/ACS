# ACS Deterministic Rule Engine (Cloud Run, CEL)

Pure CEL evaluation of merchant rule sets. There is no LLM anywhere in this service.

```
Orchestrator ── refund decisions ─┐
Messy-Order agent ── order terms ─┼─► POST /v1/evaluate ─► rule_sets/{merchant}:{rule_set} (Firestore)
Policy compiler ──────────────────┘
```

Contract: [`docs/contracts/README.md#3`](../../docs/contracts/README.md#3-deterministic-rule-engine--http-cel).
The rule-set schema, fact schemas and evaluation semantics live in
[`libs/acs_common/src/acs_common/rules.py`](../../libs/acs_common/src/acs_common/rules.py).

## Fail-safe semantics

* Every rule is evaluated. Precedence: **DENY > ESCALATE > APPROVE > default** (default `ESCALATE`).
* A rule that errors, for example on a null fact: an APPROVE rule just doesn't match, while a DENY or ESCALATE rule forces ESCALATE.
* Baseline guards (`sys_*`) are enforced for every merchant whatever the rule set says:
  * `refunds`: escalate unverified (AI-agent-asserted) claims above `unverified_escalation_minor`, default 5000.
  * `order_terms`: escalate unmatched lines, unknown customers, low extraction confidence, and price variance above the limit.
* A missing rule set, or a stored one that no longer validates, is served as baseline-only (`ESCALATE`). It is never an error that could be mistaken for approval.
* Facts are type-checked per rule set (422 on a wrong type). Unknown facts are dropped.
* Compiled rule sets are cached for `ACS_CACHE_TTL_S` (30 s), so a deploy is live everywhere within that.

## Run locally

```bash
uv sync
uv run pytest
ACS_SEED_RULES_FILE=rules.json uv run python -m acs_rule_engine.main   # rules.json: list of rule-set docs
```

| Variable | Purpose |
|---|---|
| `ACS_PROJECT_ID` | Firestore project (empty: in-memory / seed file) |
| `ACS_ALLOWED_CALLERS` | JSON list of caller service accounts (enforced when `ACS_ENV` is not `local`) |
| `ACS_CACHE_TTL_S` | Rule-set cache TTL |

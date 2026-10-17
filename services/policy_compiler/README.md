# ACS Policy-as-Code Compiler (Gemini Flash, JSON Schema output, A2A)

Turns a merchant's written policy into a validated, versioned CEL rule set.

```
Orchestrator ─A2A compile_policy─► Gemini Flash (schema-constrained) ─► validate ─► test cases ─► policy_drafts/{id}
Admin approves in console ─► Orchestrator ─A2A deploy_rules─► transaction ─► rule_sets/{merchant}:{rule_set} (+ versions/)
                                                                                 └─ read by the Rule Engine ("Deploys Rules")
```

Contract: [`docs/contracts/README.md#2`](../../docs/contracts/README.md#2-policy-as-code-compiler--a2a-v10-json-rpc).

## compile_policy

1. Gemini drafts `rules`, `default_decision`, `test_cases` and `assumptions`. The output is constrained by
   `draft_json_schema(rule_set)`, which only offers the facts that rule set declares.
2. The draft is validated exactly as the engine loads it: pydantic model, CEL parse, a fact allow-list
   (no unknown identifiers or comprehensions) and a function allow-list. Validation errors go back
   to the model for one repair attempt (`ACS_MAX_ATTEMPTS`). A draft that still fails is never stored.
3. The model's test cases run through the deterministic evaluator, and the pass/fail table is returned
   for the approver. The model never decides whether its own rules are right.
4. The draft is stored with `base_version`, the live version it was compiled against. Only a hash of
   the policy text is kept.

## deploy_rules (human-approved)

A Firestore transaction checks that the draft belongs to the merchant, is still `draft` and has not
expired. It then checks `expected_version` (or the draft's `base_version`) against the live version
(**optimistic concurrency**), re-validates the rules, and writes the live rule set plus an immutable
`versions/vN` copy. Conflicts come back as `TASK_STATE_REJECTED`.
`ACS_REQUIRE_DISTINCT_APPROVER=true` enforces four-eyes approval.

## Run locally

```bash
uv sync && uv run pytest
GOOGLE_API_KEY=... uv run python -m acs_policy_compiler.main
```

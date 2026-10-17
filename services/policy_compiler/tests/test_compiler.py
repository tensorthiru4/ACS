from __future__ import annotations

from google.genai import errors

from acs_policy_compiler.compiler import system_prompt

POLICY = (
    "Damaged items reported within 30 days get a full refund up to $200. "
    "Change-of-mind returns after 14 days are refused."
)

GOOD_OUTPUT = {
    "rules": [
        {
            "id": "damaged_recent",
            "description": "Damaged within 30 days",
            "when": "reason == 'damaged' && days_since_delivery != null "
            "&& days_since_delivery <= 30",
            "decision": "APPROVE",
            "reason": "damaged within 30 days",
            "max_refund_minor": 20000,
        },
        {
            "id": "changed_mind_late",
            "description": "Change of mind after 14 days",
            "when": "reason == 'changed_mind' && days_since_delivery > 14",
            "decision": "DENY",
            "reason": "change of mind after 14 days",
            "max_refund_minor": None,
        },
    ],
    "default_decision": "ESCALATE",
    "test_cases": [
        {
            "name": "damaged day 3",
            "facts": {
                "reason": "damaged",
                "days_since_delivery": 3,
                "facts_verified": True,
                "requested_amount_minor": 1000,
            },
            "expected_decision": "APPROVE",
        },
        {
            "name": "changed mind day 20",
            "facts": {"reason": "changed_mind", "days_since_delivery": 20},
            "expected_decision": "DENY",
        },
        {
            "name": "wrong expectation",
            "facts": {"reason": "other", "days_since_delivery": 1},
            "expected_decision": "APPROVE",
        },
    ],
    "assumptions": ["'full refund' capped at $200 = 20000 minor units"],
}


def compile_payload(**overrides):
    payload = {
        "skill": "compile_policy",
        "merchant_id": "m1",
        "policy_name": "returns-v2",
        "target_rule_set": "refunds",
        "policy_text": POLICY,
        "dry_run": True,
        "requested_by": "u-admin",
    }
    payload.update(overrides)
    return payload


async def test_compile_creates_validated_draft(a2a, gemini, repo):
    gemini.script = [GOOD_OUTPUT]
    result = await a2a.send(compile_payload())
    assert result["state"] == "TASK_STATE_COMPLETED"
    draft = result["data"][0]
    # Contract: at least draft_id, rules, version.
    assert len(draft["draft_id"]) == 32
    assert draft["version"] == "v0"
    assert draft["proposed_version"] == "v1"
    assert [r["id"] for r in draft["rules"]] == ["damaged_recent", "changed_mind_late"]
    assert draft["tests_total"] == 3 and draft["tests_passed"] == 2
    assert draft["baseline_rules"][0]["id"] == "sys_unverified_high_value"
    stored = repo.drafts[draft["draft_id"]]
    assert stored["status"] == "draft" and stored["merchant_id"] == "m1"
    assert "policy_text" not in stored  # only a hash of the text is kept
    # JSON-schema constrained output was requested.
    config = gemini.calls[0]["config"]
    assert config.response_mime_type == "application/json"
    assert "rules" in config.response_json_schema["properties"]


async def test_invalid_draft_is_repaired_once(a2a, gemini, repo):
    bad = {**GOOD_OUTPUT, "rules": [{**GOOD_OUTPUT["rules"][0], "when": "secret == 1"}]}
    gemini.script = [bad, GOOD_OUTPUT]
    result = await a2a.send(compile_payload())
    assert result["state"] == "TASK_STATE_COMPLETED"
    repair_prompt = "\n".join(str(c) for c in gemini.calls[1]["contents"])
    assert "unknown facts: secret" in repair_prompt


async def test_draft_that_never_validates_fails_and_is_not_stored(a2a, gemini, repo):
    bad = {**GOOD_OUTPUT, "rules": [{**GOOD_OUTPUT["rules"][0], "when": "now() > 1"}]}
    gemini.script = [bad, bad]
    result = await a2a.send(compile_payload())
    assert result["state"] == "TASK_STATE_FAILED"
    assert "could not be compiled" in result["text"][0]
    assert repo.drafts == {}


async def test_model_quota_error_fails_cleanly(a2a, gemini):
    gemini.script = [errors.APIError(429, {"error": {"message": "quota", "status": "RESOURCE"}})]
    result = await a2a.send(compile_payload())
    assert result["state"] == "TASK_STATE_FAILED"
    assert "try again" in result["text"][0]


async def test_bad_requests_are_rejected_without_model_calls(a2a, gemini):
    for payload in (
        compile_payload(target_rule_set="shipping"),
        compile_payload(policy_text="short"),
        compile_payload(extra="x"),
        {"skill": "drop_tables", "merchant_id": "m1"},
    ):
        result = await a2a.send(payload)
        assert result["state"] == "TASK_STATE_REJECTED", payload
    assert gemini.calls == []


async def test_deploy_flow_with_optimistic_concurrency(a2a, gemini, repo):
    gemini.script = [GOOD_OUTPUT, GOOD_OUTPUT]
    first = (await a2a.send(compile_payload()))["data"][0]
    second = (await a2a.send(compile_payload()))["data"][0]

    deploy = {
        "skill": "deploy_rules",
        "merchant_id": "m1",
        "draft_id": first["draft_id"],
        "expected_version": first["version"],
        "approved_by": "u-admin",
    }
    result = await a2a.send(deploy)
    assert result["state"] == "TASK_STATE_COMPLETED"
    assert result["data"][0]["version"] == "v1"
    live = repo.rule_sets["m1:refunds"]
    assert live["version"] == "v1" and live["deployed_by"] == "u-admin"

    # Redeploying the same draft is refused.
    assert (await a2a.send(deploy))["state"] == "TASK_STATE_REJECTED"
    # The second draft was compiled against v0, so it conflicts now.
    stale = {**deploy, "draft_id": second["draft_id"], "expected_version": None}
    conflict = await a2a.send(stale)
    assert conflict["state"] == "TASK_STATE_REJECTED"
    assert "v1" in conflict["text"][0]
    # Explicitly targeting the current version succeeds.
    fresh = await a2a.send({**stale, "expected_version": "v1"})
    assert fresh["data"][0]["version"] == "v2"
    assert len(repo.history["m1:refunds"]) == 2


async def test_deploy_is_tenant_scoped(a2a, gemini):
    gemini.script = [GOOD_OUTPUT]
    draft = (await a2a.send(compile_payload()))["data"][0]
    result = await a2a.send(
        {
            "skill": "deploy_rules",
            "merchant_id": "m2",
            "draft_id": draft["draft_id"],
            "expected_version": "v0",
            "approved_by": "u-x",
        }
    )
    assert result["state"] == "TASK_STATE_REJECTED"
    assert "not found" in result["text"][0]


async def test_get_task_after_completion(a2a, gemini):
    gemini.script = [GOOD_OUTPUT]
    result = await a2a.send(compile_payload())
    fetched = await a2a.get_task(result["task_id"])
    assert fetched["state"] == "TASK_STATE_COMPLETED"
    assert fetched["data"][0]["draft_id"] == result["data"][0]["draft_id"]


def test_prompt_lists_only_rule_set_facts():
    prompt = system_prompt("order_terms")
    assert "unmatched_line_count" in prompt
    assert "requested_amount_minor" not in prompt
    assert "sys_unmatched_lines" in prompt

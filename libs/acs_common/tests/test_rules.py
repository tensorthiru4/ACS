from __future__ import annotations

import pytest

from acs_common.rules import (
    Decision,
    FactValidationError,
    RuleSet,
    RuleValidationError,
    compile_rule_set,
    draft_json_schema,
    evaluate,
    normalize_facts,
)


def refund_facts(**overrides):
    facts = {
        "order_id": "o1",
        "requested_amount_minor": 2599,
        "currency": "USD",
        "reason": "damaged",
        "days_since_delivery": 3,
        "item_condition": "damaged",
        "channel": "merchant_user",
        "facts_verified": True,
    }
    facts.update(overrides)
    return normalize_facts("refunds", facts)


def refunds(*rules, **kwargs):
    return compile_rule_set(
        RuleSet(merchant_id="m1", rule_set="refunds", version="v3", rules=list(rules), **kwargs)
    )


APPROVE_DAMAGED = {
    "id": "damaged_recent",
    "when": "reason == 'damaged' && days_since_delivery <= 30",
    "decision": "APPROVE",
    "reason": "damaged within 30 days",
    "max_refund_minor": 2000,
}
DENY_CHANGED_MIND = {
    "id": "changed_mind_late",
    "when": "reason == 'changed_mind' && days_since_delivery > 14",
    "decision": "DENY",
    "reason": "change of mind after 14 days",
}


def test_approve_caps_refund():
    result = evaluate(refunds(APPROVE_DAMAGED), refund_facts())
    assert result.decision == Decision.APPROVE
    assert result.max_refund_minor == 2000
    assert result.matched_rule_ids == ["damaged_recent"]


def test_no_match_uses_default_escalate():
    result = evaluate(refunds(APPROVE_DAMAGED), refund_facts(reason="other"))
    assert result.decision == Decision.ESCALATE
    assert result.matched_rule_ids == []


def test_deny_beats_approve():
    always = {"id": "always", "when": "true", "decision": "APPROVE", "reason": "x"}
    result = evaluate(
        refunds(always, DENY_CHANGED_MIND),
        refund_facts(reason="changed_mind", days_since_delivery=20),
    )
    assert result.decision == Decision.DENY


def test_missing_fact_in_approve_rule_does_not_match():
    result = evaluate(refunds(APPROVE_DAMAGED), refund_facts(days_since_delivery=None))
    assert result.decision == Decision.ESCALATE
    assert "damaged_recent" in result.errored_rule_ids


def test_missing_fact_in_deny_rule_forces_escalate():
    always = {"id": "always", "when": "true", "decision": "APPROVE", "reason": "x"}
    result = evaluate(
        refunds(always, DENY_CHANGED_MIND),
        refund_facts(reason="changed_mind", days_since_delivery=None),
    )
    assert result.decision == Decision.ESCALATE
    assert any("changed_mind_late" in r for r in result.reasons)


def test_baseline_guard_escalates_unverified_high_value():
    always = {"id": "always", "when": "true", "decision": "APPROVE", "reason": "x"}
    compiled = refunds(always)
    low = evaluate(compiled, refund_facts(facts_verified=False, requested_amount_minor=4000))
    high = evaluate(compiled, refund_facts(facts_verified=False, requested_amount_minor=9000))
    assert low.decision == Decision.APPROVE
    assert high.decision == Decision.ESCALATE
    assert "sys_unverified_high_value" in high.matched_rule_ids


def test_baseline_threshold_is_tunable_but_bounded():
    always = {"id": "always", "when": "true", "decision": "APPROVE", "reason": "x"}
    compiled = refunds(always, params={"unverified_escalation_minor": 100})
    result = evaluate(compiled, refund_facts(facts_verified=False, requested_amount_minor=500))
    assert result.decision == Decision.ESCALATE
    with pytest.raises(RuleValidationError):
        refunds(always, params={"unverified_escalation_minor": -1})
    with pytest.raises(RuleValidationError):
        refunds(always, params={"made_up": 1})


@pytest.mark.parametrize(
    ("expression", "problem"),
    [
        ("reason == ", "Syntax"),
        ("secret_field == 1", "unknown facts"),
        ("[1, 2].exists(i, i > requested_amount_minor)", "unknown facts"),
        ("timestamp('2020-01-01T00:00:00Z') > timestamp('2019-01-01T00:00:00Z')", "functions"),
    ],
)
def test_invalid_expressions_rejected(expression, problem):
    rule = {"id": "r1", "when": expression, "decision": "APPROVE", "reason": "x"}
    with pytest.raises(RuleValidationError) as info:
        refunds(rule)
    assert problem.lower() in str(info.value).lower()


def test_reserved_and_duplicate_ids_rejected():
    rule = {"id": "sys_x", "when": "true", "decision": "APPROVE", "reason": "x"}
    with pytest.raises(ValueError, match="reserved"):
        RuleSet(merchant_id="m1", rule_set="refunds", rules=[rule])
    dup = {"id": "a", "when": "true", "decision": "APPROVE", "reason": "x"}
    with pytest.raises(ValueError, match="unique"):
        RuleSet(merchant_id="m1", rule_set="refunds", rules=[dup, dup])


def test_max_refund_only_on_refund_approvals():
    rule = {**DENY_CHANGED_MIND, "max_refund_minor": 5}
    with pytest.raises(RuleValidationError, match="max_refund_minor"):
        refunds(rule)


def test_fact_types_are_enforced():
    with pytest.raises(FactValidationError):
        normalize_facts("refunds", {"requested_amount_minor": "2599"})
    with pytest.raises(FactValidationError):
        normalize_facts("refunds", {"facts_verified": 1})
    facts = normalize_facts("refunds", {"unknown": 1, "reason": "x" * 1000})
    assert "unknown" not in facts
    assert len(facts["reason"]) == 256


def test_order_terms_baseline_guards():
    approve_small = {
        "id": "small_orders",
        "when": "order_total_minor <= 500000",
        "decision": "APPROVE",
        "reason": "small order from a known customer",
    }
    compiled = compile_rule_set(
        RuleSet(merchant_id="m1", rule_set="order_terms", rules=[approve_small])
    )
    good = normalize_facts(
        "order_terms",
        {
            "order_total_minor": 1000,
            "unmatched_line_count": 0,
            "customer_known": True,
            "extraction_confidence": 0.95,
            "max_price_variance_pct": 0.0,
        },
    )
    assert evaluate(compiled, good).decision == Decision.APPROVE
    for override in (
        {"unmatched_line_count": 1},
        {"customer_known": False},
        {"extraction_confidence": 0.4},
        {"max_price_variance_pct": 12.5},
        {"customer_known": None},
    ):
        bad = {**good, **override}
        assert evaluate(compiled, bad).decision == Decision.ESCALATE, override


def test_draft_schema_lists_rule_set_facts():
    schema = draft_json_schema("order_terms")
    facts = schema["properties"]["test_cases"]["items"]["properties"]["facts"]["properties"]
    assert "unmatched_line_count" in facts
    assert "requested_amount_minor" not in facts

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from acs_rule_engine.config import Settings
from acs_rule_engine.engine import MemoryRuleSets, RuleEngine
from acs_rule_engine.main import create_app

REFUNDS_V7 = {
    "merchant_id": "m1",
    "rule_set": "refunds",
    "version": "v7",
    "default_decision": "ESCALATE",
    "rules": [
        {
            "id": "damaged_recent",
            "when": "reason == 'damaged' && days_since_delivery <= 30",
            "decision": "APPROVE",
            "reason": "damaged within 30 days",
            "max_refund_minor": 2000,
        },
        {
            "id": "changed_mind_late",
            "when": "reason == 'changed_mind' && days_since_delivery > 14",
            "decision": "DENY",
            "reason": "change of mind after 14 days",
        },
    ],
}

FACTS = {
    "order_id": "ord-1",
    "requested_amount_minor": 2599,
    "currency": "USD",
    "reason": "damaged",
    "days_since_delivery": 3,
    "item_condition": "damaged",
    "channel": "merchant_user",
    "facts_verified": True,
}


@pytest.fixture
def source() -> MemoryRuleSets:
    return MemoryRuleSets({"m1:refunds": REFUNDS_V7})


@pytest.fixture
def client(source):
    settings = Settings(env="local")
    app = create_app(settings, RuleEngine(source, cache_ttl_s=0))
    with TestClient(app) as c:
        yield c


def call(client, facts=None, merchant="m1", rule_set="refunds"):
    return client.post(
        "/v1/evaluate",
        json={
            "merchant_id": merchant,
            "rule_set": rule_set,
            "request_id": "req-1",
            "facts": facts if facts is not None else FACTS,
        },
    )


def test_contract_response(client):
    response = call(client)
    assert response.status_code == 200
    assert response.json() == {
        "decision": "APPROVE",
        "reasons": ["damaged within 30 days"],
        "matched_rule_ids": ["damaged_recent"],
        "rule_set_version": "v7",
        "max_refund_minor": 2000,
    }


def test_deny(client):
    body = call(client, {**FACTS, "reason": "changed_mind", "days_since_delivery": 20}).json()
    assert body["decision"] == "DENY"


def test_unverified_high_value_escalates(client):
    body = call(client, {**FACTS, "facts_verified": False, "requested_amount_minor": 99_000}).json()
    assert body["decision"] == "ESCALATE"
    assert "sys_unverified_high_value" in body["matched_rule_ids"]


def test_missing_rule_set_escalates(client):
    body = call(client, merchant="m-unknown").json()
    assert body["decision"] == "ESCALATE"
    assert body["rule_set_version"] == ""


def test_invalid_stored_rule_set_fails_safe(client, source):
    source.docs["m1:refunds"] = {
        **REFUNDS_V7,
        "rules": [{"id": "x", "when": "true ||", "decision": "APPROVE", "reason": "x"}],
    }
    body = call(client).json()
    assert body["decision"] == "ESCALATE"
    assert body["rule_set_version"] == "v7!invalid"


def test_rule_set_document_must_match_name(client, source):
    source.docs["m1:refunds"] = {**REFUNDS_V7, "rule_set": "pricing"}
    assert call(client).json()["decision"] == "ESCALATE"


def test_wrong_fact_type_is_422(client):
    response = call(client, {**FACTS, "requested_amount_minor": "2599"})
    assert response.status_code == 422
    assert "requested_amount_minor" in response.json()["error"]


def test_request_validation(client):
    assert call(client, merchant="../etc").status_code == 422
    assert call(client, rule_set="shipping").status_code == 422
    too_many = {f"k{i}": 1 for i in range(65)}
    assert call(client, too_many).status_code == 422


def test_caller_allow_list_enforced_outside_local(source, monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    settings = Settings(env="prod", allowed_callers=["acs-orchestrator@p.iam.gserviceaccount.com"])
    app = create_app(settings, RuleEngine(source))
    with TestClient(app) as c:
        response = call(c)
    assert response.status_code == 403


def test_cache_serves_new_version_after_ttl(source):
    import asyncio

    engine = RuleEngine(source, cache_ttl_s=60)
    first, v1 = asyncio.run(engine.evaluate("m1", "refunds", FACTS))
    source.docs["m1:refunds"] = {**REFUNDS_V7, "version": "v8"}
    _, still = asyncio.run(engine.evaluate("m1", "refunds", FACTS))
    assert (v1, still) == ("v7", "v7")
    _, fresh = asyncio.run(RuleEngine(source, cache_ttl_s=0).evaluate("m1", "refunds", FACTS))
    assert fresh == "v8"

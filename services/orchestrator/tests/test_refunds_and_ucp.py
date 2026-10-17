from __future__ import annotations

from conftest import ADMIN, AGENT, VIEWER

REFUND = {
    "order_id": "ord-1001",
    "amount": {"amount_minor": 2599, "currency": "USD"},
    "reason": "damaged",
    "days_since_delivery": 3,
    "item_condition": "damaged",
}
UCP_AGENT = {"UCP-Agent": 'profile="https://agent.example/profile.json"'}


def test_merchant_refund_uses_rule_engine_and_clamps_amount(harness):
    r = harness.client.post("/v1/refunds/decide", json=REFUND, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["decision"] == "APPROVE"
    assert body["approved_amount"] == {"amount_minor": 2000, "currency": "USD"}
    assert body["source"] == "rule_engine"
    call = harness.rule_calls[0]
    assert call["merchant_id"] == "m1"
    assert call["rule_set"] == "refunds"
    assert call["facts"]["facts_verified"] is True
    assert call["facts"]["channel"] == "merchant_user"


def test_viewer_cannot_decide_refunds(harness):
    assert harness.client.post("/v1/refunds/decide", json=REFUND, headers=VIEWER).status_code == 403


def test_rule_engine_failure_fails_safe_to_escalate(harness):
    harness.rule_status = 500
    body = harness.client.post("/v1/refunds/decide", json=REFUND, headers=ADMIN).json()
    assert body["decision"] == "ESCALATE"
    assert body["source"] == "failsafe"
    assert body["approved_amount"] is None


def test_malformed_rule_engine_payload_fails_safe(harness):
    harness.rule_response = {"decision": "MAYBE"}
    body = harness.client.post("/v1/refunds/decide", json=REFUND, headers=ADMIN).json()
    assert body["decision"] == "ESCALATE"


def test_refund_idempotency(harness):
    h = {**ADMIN, "Idempotency-Key": "key-1"}
    first = harness.client.post("/v1/refunds/decide", json=REFUND, headers=h).json()
    second = harness.client.post("/v1/refunds/decide", json=REFUND, headers=h).json()
    assert first["decision_id"] == second["decision_id"]
    assert len(harness.rule_calls) == 1
    changed = {**REFUND, "amount": {"amount_minor": 1, "currency": "USD"}}
    assert harness.client.post("/v1/refunds/decide", json=changed, headers=h).status_code == 409


def test_ucp_profile_is_public_and_cacheable(harness):
    r = harness.client.get("/.well-known/ucp")
    assert r.status_code == 200
    profile = r.json()
    assert "dev.ucp.shopping" in profile["ucp"]["services"]
    assert profile["ucp"]["payment_handlers"] == {}
    assert "public" in r.headers["cache-control"]
    assert (
        harness.client.get("/ucp/v1/schemas/refund_request.json").json()["title"] == "RefundRequest"
    )


def test_ucp_refund_requires_matching_agent_profile(harness):
    c = harness.client
    assert c.post("/ucp/v1/refunds", json=REFUND, headers=AGENT).status_code == 422
    mismatch = {**AGENT, "UCP-Agent": 'profile="https://impostor.example/p.json"'}
    assert c.post("/ucp/v1/refunds", json=REFUND, headers=mismatch).status_code == 401
    r = c.post("/ucp/v1/refunds", json=REFUND, headers={**AGENT, **UCP_AGENT})
    assert r.status_code == 200, r.text
    facts = harness.rule_calls[-1]["facts"]
    assert facts["channel"] == "shopper_agent"
    assert facts["facts_verified"] is False


def test_unknown_fields_rejected(harness):
    body = {**REFUND, "merchant_id": "m2"}
    r = harness.client.post("/ucp/v1/refunds", json=body, headers={**AGENT, **UCP_AGENT})
    assert r.status_code == 422

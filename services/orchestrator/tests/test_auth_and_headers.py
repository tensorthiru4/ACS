from __future__ import annotations

import json

from conftest import ADMIN, AGENT, ANALYST, VIEWER, bearer


def test_missing_and_invalid_credentials_are_rejected(harness):
    c = harness.client
    assert c.post("/v1/console/sessions").status_code == 401
    assert c.post("/v1/console/sessions", headers=bearer("nope")).status_code == 401
    r = c.post("/v1/console/sessions", headers=bearer("no-claims-token"))
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Bearer")
    assert r.json()["error"] == "unauthorized"


def test_client_credentials(harness):
    c = harness.client
    body = {
        "order_id": "o1",
        "amount": {"amount_minor": 100, "currency": "USD"},
        "reason": "damaged",
    }
    ucp = {"UCP-Agent": 'profile="https://agent.example/profile.json"'}
    assert (
        c.post(
            "/ucp/v1/refunds", json=body, headers={**bearer("acsk_agent1.wrong"), **ucp}
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/ucp/v1/refunds", json=body, headers={**bearer("acsk_disabled1.s3cret"), **ucp}
        ).status_code
        == 401
    )
    assert (
        c.post(
            "/ucp/v1/refunds", json=body, headers={**bearer("acsk_unknown.s3cret"), **ucp}
        ).status_code
        == 401
    )
    # A shopper agent credential is not accepted on merchant endpoints.
    assert c.post("/v1/console/sessions", headers=AGENT).status_code == 401


def test_gateway_forwarded_authorization_is_preferred(harness):
    # API Gateway puts its own token in Authorization and the caller's in X-Forwarded-Authorization.
    headers = {
        "Authorization": "Bearer gateway-google-token",
        "X-Forwarded-Authorization": "Bearer admin-token",
    }
    assert harness.client.post("/v1/console/sessions", headers=headers).status_code == 201


def test_rbac_by_role(harness):
    c = harness.client
    policy = {
        "policy_name": "returns",
        "policy_text": "Refunds allowed within 30 days of delivery.",
    }
    assert c.post("/v1/policies/compile", json=policy, headers=VIEWER).status_code == 403
    assert c.post("/v1/policies/compile", json=policy, headers=ANALYST).status_code == 403
    assert c.post("/v1/policies/compile", json=policy, headers=ADMIN).status_code == 200
    assert harness.compiler_exec.received[0]["merchant_id"] == "m1"


def test_dev_principal_header_refused_on_cloud_run(harness, monkeypatch):
    dev = {
        "X-ACS-Dev-Principal": json.dumps(
            {"kind": "merchant_user", "subject": "u1", "merchant_id": "m1", "role": "admin"}
        )
    }
    assert harness.client.post("/v1/console/sessions", headers=dev).status_code == 201
    monkeypatch.setenv("K_SERVICE", "acs-orchestrator")
    assert harness.client.post("/v1/console/sessions", headers=dev).status_code == 401


def test_security_headers_and_request_id(harness):
    r = harness.client.get("/health", headers={"X-Request-Id": "abc-123"})
    assert r.status_code == 200
    assert r.headers["x-request-id"] == "abc-123"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert "max-age" in r.headers["strict-transport-security"]
    # Malformed request ids are replaced.
    r = harness.client.get("/health", headers={"X-Request-Id": "bad/id;drop"})
    assert r.headers["x-request-id"] != "bad/id;drop"


def test_oversized_body_rejected(harness):
    big = b"x" * (harness.settings.max_request_bytes + 1)
    r = harness.client.post(
        "/v1/intake/uploads",
        content=big,
        headers={**ANALYST, "Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 413


def test_validation_errors_do_not_echo_input(harness):
    r = harness.client.post("/v1/refunds/decide", json={"order_id": "<script>"}, headers=ADMIN)
    assert r.status_code == 422
    assert "<script>" not in r.text

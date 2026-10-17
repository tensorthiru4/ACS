"""Orchestrator -> Auditor / Policy Compiler over real A2A v1.0 JSON-RPC."""

from __future__ import annotations

from conftest import ADMIN, ANALYST, OTHER_TENANT


def test_audit_start_and_poll(harness):
    r = harness.client.post(
        "/v1/audits", json={"store_url": "https://shop.example.com/"}, headers=ANALYST
    )
    assert r.status_code == 202, r.text
    view = r.json()
    assert view["agent"] == "auditor"
    assert view["task_id"]
    sent = harness.auditor_exec.received[0]
    assert sent["skill"] == "audit_store"
    assert sent["merchant_id"] == "m1"
    assert sent["store_url"] == "https://shop.example.com/"

    poll = harness.client.get(f"/v1/audits/{view['task_id']}", headers=ANALYST)
    assert poll.status_code == 200, poll.text
    assert poll.json()["result"]["data"][0]["score"] == 72
    # Task ids are tenant-scoped.
    assert (
        harness.client.get(f"/v1/audits/{view['task_id']}", headers=OTHER_TENANT).status_code == 404
    )


def test_audit_url_validation(harness):
    for url in [
        "http://shop.example.com/",  # not https
        "https://evil.example.net/",  # not a registered storefront
        "https://169.254.169.254/latest",  # metadata IP literal
        "https://user:pw@shop.example.com/",  # credentials
        "https://shop.example.com:8443/",  # odd port
        "https://shop.example.com.evil.net/",  # suffix trick
    ]:
        r = harness.client.post("/v1/audits", json={"store_url": url}, headers=ANALYST)
        assert r.status_code == 422, url
    assert harness.auditor_exec.received == []


def test_policy_compile_then_human_deploy(harness):
    policy = {
        "policy_name": "returns-v2",
        "policy_text": "Damaged items can be refunded within 30 days of delivery.",
    }
    r = harness.client.post("/v1/policies/compile", json=policy, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["data"][0]["draft_id"] == "draft-123"
    assert harness.compiler_exec.received[0]["dry_run"] is True

    assert (
        harness.client.post(
            "/v1/policies/draft-123/deploy", json={}, headers=OTHER_TENANT
        ).status_code
        == 404
    )
    assert (
        harness.client.post("/v1/policies/unknown/deploy", json={}, headers=ADMIN).status_code
        == 404
    )
    r = harness.client.post(
        "/v1/policies/draft-123/deploy", json={"expected_version": "v7"}, headers=ADMIN
    )
    assert r.status_code == 200, r.text
    deploy = harness.compiler_exec.received[-1]
    assert deploy == {
        "skill": "deploy_rules",
        "merchant_id": "m1",
        "draft_id": "draft-123",
        "expected_version": "v7",
        "approved_by": "u-admin",
    }


def test_downstream_unavailable_maps_to_502(harness):
    harness.ops.auditor = None
    r = harness.client.post(
        "/v1/audits", json={"store_url": "https://shop.example.com/"}, headers=ANALYST
    )
    assert r.status_code == 502

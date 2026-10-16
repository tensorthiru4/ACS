from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from conftest import ADMIN, ANALYST, OTHER_TENANT, PARTNER, VIEWER

from acs_orchestrator.clients.order_desk import OrderDeskClient

DRAFT_ID = "c" * 40


@pytest.fixture
def desk(harness):
    calls: list[tuple[str, dict]] = []
    state = {"status": 200, "body": {"intake_id": DRAFT_ID, "status": "approved"}}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(state["status"], json=state["body"])

    harness.ops.order_desk = OrderDeskClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://orders")
    )
    harness.store.data["draft_sales_orders"] = {
        DRAFT_ID: {
            "intake_id": DRAFT_ID,
            "merchant_id": "m1",
            "status": "needs_review",
            "po_number": "PO-1",
            "currency": "USD",
            "total_minor": 9000,
            "lines": [{"line_no": 1, "sku": None}],
            "raw_po_uri": "gs://bucket/m1/x",
            "facts": {"order_total_minor": 9000},
            "created_at": datetime(2026, 10, 16, tzinfo=UTC),
        },
        "d" * 40: {"intake_id": "d" * 40, "merchant_id": "m2", "status": "approved"},
    }
    harness.desk_calls = calls
    harness.desk_state = state
    return harness


def test_list_and_get_are_tenant_scoped(desk):
    listed = desk.client.get("/v1/orders/drafts", headers=VIEWER)
    assert listed.status_code == 200
    assert [d["intake_id"] for d in listed.json()] == [DRAFT_ID]
    assert desk.client.get("/v1/orders/drafts?status=posted", headers=VIEWER).json() == []

    detail = desk.client.get(f"/v1/orders/drafts/{DRAFT_ID}", headers=VIEWER).json()
    assert detail["status"] == "needs_review"
    assert "raw_po_uri" not in detail and "facts" not in detail

    assert desk.client.get(f"/v1/orders/drafts/{DRAFT_ID}", headers=OTHER_TENANT).status_code == 404
    assert desk.client.get(f"/v1/orders/drafts/{'d' * 40}", headers=ADMIN).status_code == 404
    assert desk.client.get("/v1/orders/drafts/not-an-id", headers=ADMIN).status_code == 404


def test_release_requires_admin_and_relays(desk):
    body = {"line_overrides": [{"line_no": 1, "sku": "GAD-RED-S"}]}
    url = f"/v1/orders/drafts/{DRAFT_ID}/release"
    assert desk.client.post(url, json=body, headers=ANALYST).status_code == 403
    assert desk.client.post(url, json=body, headers=PARTNER).status_code == 401
    assert desk.client.post(url, json=body, headers=OTHER_TENANT).status_code == 404
    assert desk.desk_calls == []

    response = desk.client.post(url, json=body, headers=ADMIN)
    assert response.status_code == 200
    path, sent = desk.desk_calls[0]
    assert path == f"/v1/drafts/{DRAFT_ID}/release"
    assert sent == {
        "merchant_id": "m1",
        "approved_by": "m1:merchant_user:u-admin",
        "line_overrides": [{"line_no": 1, "sku": "GAD-RED-S"}],
        "customer_ref": None,
    }


def test_release_maps_agent_refusals(desk):
    url = f"/v1/orders/drafts/{DRAFT_ID}/release"
    desk.desk_state.update(status=409, body={"error": "draft is incomplete: line 1"})
    conflict = desk.client.post(url, json={}, headers=ADMIN)
    assert conflict.status_code == 409 and "line 1" in conflict.json()["error"]
    desk.desk_state.update(status=500, body={"error": "boom"})
    assert desk.client.post(url, json={}, headers=ADMIN).status_code == 502


def test_reject(desk):
    desk.desk_state["body"] = {"intake_id": DRAFT_ID, "status": "rejected"}
    response = desk.client.post(
        f"/v1/orders/drafts/{DRAFT_ID}/reject", json={"reason": "duplicate"}, headers=ADMIN
    )
    assert response.status_code == 200
    assert desk.desk_calls[0][1]["rejected_by"] == "m1:merchant_user:u-admin"


def test_release_without_agent_is_502(harness):
    harness.store.data["draft_sales_orders"] = {DRAFT_ID: {"merchant_id": "m1"}}
    response = harness.client.post(f"/v1/orders/drafts/{DRAFT_ID}/release", json={}, headers=ADMIN)
    assert response.status_code == 502


def test_release_body_is_strict(desk):
    url = f"/v1/orders/drafts/{DRAFT_ID}/release"
    for body in (
        {"approved_by": "someone-else"},
        {"line_overrides": [{"line_no": 0, "sku": "X"}]},
        {"customer_ref": "a/b"},
    ):
        assert desk.client.post(url, json=body, headers=ADMIN).status_code == 422

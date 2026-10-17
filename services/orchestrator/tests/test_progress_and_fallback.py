"""Console progress polling and the Gemini -> Gemma model fallback."""

from __future__ import annotations

from conftest import ADMIN, ANALYST, OTHER_TENANT, ScriptedLlm
from google.adk.models import LlmRequest
from google.genai import errors as genai_errors
from google.genai import types

from acs_orchestrator.agents import progress
from acs_orchestrator.agents.models import FallbackLlm
from acs_orchestrator.config import Settings
from acs_orchestrator.container import agent_model

REFUND_CALL = {
    "name": "request_refund_decision",
    "args": {
        "order_id": "1001",
        "amount_minor": 290000,
        "currency": "INR",
        "reason": "damaged",
        "days_since_delivery": 3,
        "item_condition": "damaged",
    },
}
QUOTA = genai_errors.ClientError(
    429,
    {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"retryDelay": "13023s"}],
        }
    },
)


PID = "0b6f5c1e-8d2a-4b7e-9a51-3c2d1e0f9a87"


def _session(client, headers) -> str:
    return client.post("/v1/console/sessions", headers=headers).json()["session_id"]


def _progress(client, headers, pid=PID):
    return client.get(f"/v1/console/progress/{pid}", headers=headers)


# --------------------------------------------------------------------------- progress
def test_chat_progress_reports_tools_then_finishes(harness):
    sid = _session(harness.client, ANALYST)
    harness.llm.script = [REFUND_CALL, "Approved: refund of 2,900.00 INR."]
    r = harness.client.post(
        f"/v1/console/sessions/{sid}/messages",
        json={"text": "Order 1001 arrived with 2 broken bottles"},
        headers={**ANALYST, "X-Progress-Id": PID},
    )
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Approved: refund of 2,900.00 INR."
    assert r.json()["tool_calls"] == ["request_refund_decision"]

    progress = _progress(harness.client, ANALYST).json()
    assert progress["done"] is True
    events = progress["events"]
    assert events[0]["type"] == "status"
    assert {"type": "tool_call", "name": "request_refund_decision"}.items() <= events[1].items()
    assert any(e["type"] == "tool_result" and e["ok"] for e in events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert all("elapsed_s" in e for e in events)


def test_failed_call_returns_its_error_and_marks_progress_done(harness):
    sid = _session(harness.client, ANALYST)
    harness.llm.script = [QUOTA]
    r = harness.client.post(
        f"/v1/console/sessions/{sid}/messages",
        json={"text": "hi"},
        headers={**ANALYST, "X-Progress-Id": PID},
    )
    assert r.status_code == 503
    assert "out of quota" in r.json()["error"]
    assert _progress(harness.client, ANALYST).json()["done"] is True


def test_progress_is_private_to_its_caller(harness):
    sid = _session(harness.client, ANALYST)
    harness.client.post(
        f"/v1/console/sessions/{sid}/messages",
        json={"text": "hi"},
        headers={**ANALYST, "X-Progress-Id": PID},
    )
    assert _progress(harness.client, ADMIN).status_code == 404
    assert _progress(harness.client, OTHER_TENANT).status_code == 404
    # Nor can another user write under the same id.
    r = harness.client.post(
        "/v1/policies/compile",
        json={"policy_name": "returns-v2", "policy_text": "Refund damaged items in 30 days."},
        headers={**ADMIN, "X-Progress-Id": PID},
    )
    assert r.status_code == 409


def test_unknown_or_malformed_progress_ids(harness):
    assert _progress(harness.client, ANALYST).status_code == 404
    assert _progress(harness.client, ANALYST, "x").status_code == 404
    sid = _session(harness.client, ANALYST)
    r = harness.client.post(
        f"/v1/console/sessions/{sid}/messages",
        json={"text": "hi"},
        headers={**ANALYST, "X-Progress-Id": "../../etc"},
    )
    assert r.status_code == 422


def test_without_a_progress_id_nothing_is_recorded(harness):
    sid = _session(harness.client, ANALYST)
    r = harness.client.post(
        f"/v1/console/sessions/{sid}/messages", json={"text": "hi"}, headers=ANALYST
    )
    assert r.status_code == 200
    assert harness.store.data.get("console_progress", {}) == {}


def test_compile_progress(harness):
    r = harness.client.post(
        "/v1/policies/compile",
        json={"policy_name": "returns-v2", "policy_text": "Refund damaged items in 30 days."},
        headers={**ADMIN, "X-Progress-Id": PID},
    )
    assert r.status_code == 200, r.text
    assert r.json()["result"]["data"][0]["draft_id"] == "draft-123"
    progress = _progress(harness.client, ADMIN).json()
    assert progress["done"] is True
    assert progress["events"][0]["text"] == "Sending the policy to the compiler agent"


def test_compile_checks_the_role_first(harness):
    r = harness.client.post(
        "/v1/policies/compile",
        json={"policy_name": "p", "policy_text": "Refund damaged items."},
        headers={**ANALYST, "X-Progress-Id": PID},
    )
    assert r.status_code == 403
    assert _progress(harness.client, ANALYST).status_code == 404


# --------------------------------------------------------------------------- fallback
def _request() -> LlmRequest:
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="refund 1001")])],
        config=types.GenerateContentConfig(
            system_instruction="be brief",
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="request_refund_decision",
                            description="decide",
                            parameters_json_schema={"type": "object", "properties": {}},
                        )
                    ]
                )
            ],
        ),
    )


async def _collect(llm, request):
    return [r async for r in llm.generate_content_async(request)]


async def test_quota_error_falls_back_to_gemma_and_cools_down():
    primary = ScriptedLlm(model="gemini-3.8-flash", script=[QUOTA], requests=[])
    gemma = ScriptedLlm(model="gemma-4-31b-it", script=["one", "two"], requests=[])
    llm = FallbackLlm(primary=primary, fallback=gemma)
    notes: list[dict] = []

    with progress.reporting_to(notes.append):
        first = await _collect(llm, _request())
        second = await _collect(llm, _request())

    assert first[0].content.parts[0].text == "one"
    assert second[0].content.parts[0].text == "two"
    assert len(primary.requests) == 1  # skipped during the cool-down
    assert notes[0]["type"] == "model" and "out of quota" in notes[0]["text"]
    assert "gemma-4-31b-it" in notes[0]["text"]
    # The same turn (system instruction and tools) goes to Gemma unchanged.
    assert gemma.requests[0] is primary.requests[0]
    assert llm.model == "gemini-3.8-flash"


async def test_both_models_down_surfaces_the_gemini_error():
    primary = ScriptedLlm(script=[QUOTA], requests=[])
    gemma = ScriptedLlm(script=[genai_errors.ClientError(429, {"error": {}})], requests=[])
    try:
        await _collect(FallbackLlm(primary=primary, fallback=gemma), _request())
    except genai_errors.APIError as exc:
        assert exc is QUOTA
    else:
        raise AssertionError("expected the Gemini error")


async def test_non_transient_gemini_errors_do_not_fall_back():
    primary = ScriptedLlm(script=[genai_errors.ClientError(400, {"error": {}})], requests=[])
    gemma = ScriptedLlm(requests=[])
    try:
        await _collect(FallbackLlm(primary=primary, fallback=gemma), _request())
    except genai_errors.APIError as exc:
        assert exc.code == 400
    assert gemma.requests == []


def test_fallback_model_is_configurable():
    model = agent_model(Settings(env="local"))
    assert isinstance(model, FallbackLlm)
    assert model.fallback.model == "gemma-4-31b-it"
    model = agent_model(Settings(env="local", fallback_model="gemma-4-26b-a4b-it"))
    assert model.fallback.model == "gemma-4-26b-a4b-it"
    assert not isinstance(agent_model(Settings(env="local", fallback_model="")), FallbackLlm)


async def test_progress_is_published_while_the_work_runs(monkeypatch):
    import asyncio

    from acs_orchestrator.api import live_progress
    from acs_orchestrator.clients.store import MemoryStore

    monkeypatch.setattr(live_progress, "HEARTBEAT_S", 0.05)
    monkeypatch.setattr(live_progress, "MIN_WRITE_INTERVAL_S", 0.01)
    store = MemoryStore()
    seen: list[dict] = []

    async def work(emit):
        emit({"type": "status", "text": "Thinking"})
        await asyncio.sleep(0.3)
        seen.append(await live_progress.read_progress(store, "u1", PID))
        return "ok"

    assert await live_progress.run_tracked(store, "u1", PID, work) == "ok"
    mid = seen[0]
    assert mid["done"] is False
    assert mid["events"][0]["text"] == "Thinking"
    assert mid["elapsed_s"] > 0.1  # heartbeats kept it fresh with no new events
    assert (await live_progress.read_progress(store, "u1", PID))["done"] is True

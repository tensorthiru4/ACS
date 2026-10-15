"""Test fixtures: a full orchestrator wired to fakes that speak the real protocols.

* Downstream A2A agents are real a2a-sdk servers reached through httpx ASGI transport.
* The rule engine is an httpx MockTransport.
* The LLM is a scripted ADK BaseLlm, so ADK runs tools and callbacks for real.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable
from typing import Any

import httpx
import pytest
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    Part,
    Task,
    TaskState,
    TaskStatus,
)
from fastapi.testclient import TestClient
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from google.protobuf import json_format, struct_pb2
from starlette.applications import Starlette

from acs_orchestrator.agents.context import bind_operations
from acs_orchestrator.agents.orchestrator_agent import (
    build_orchestrator_agent,
    build_shopper_agent,
)
from acs_orchestrator.clients.a2a_client import A2AAgentClient
from acs_orchestrator.clients.pubsub import MemoryPoPublisher
from acs_orchestrator.clients.rule_engine import RuleEngineClient
from acs_orchestrator.clients.store import MemoryStore
from acs_orchestrator.config import Settings
from acs_orchestrator.container import Container
from acs_orchestrator.main import create_app
from acs_orchestrator.operations import Operations
from acs_orchestrator.security.auth import Authenticator, hash_client_secret
from acs_orchestrator.security.principals import AuthError
from acs_orchestrator.security.ratelimit import RateLimiter

PEPPER = "p" * 40
BASE_URL = "https://acs.example.com"


# --------------------------------------------------------------------------- fake A2A agents
class EchoSkillExecutor(AgentExecutor):
    """Completes every task with one data artifact describing what it received."""

    def __init__(self, extra: dict[str, Any] | None = None, *, finish: bool = True) -> None:
        self.extra = extra or {}
        self.finish = finish
        self.received: list[dict[str, Any]] = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        payload: dict[str, Any] = {}
        for part in context.message.parts:
            if part.WhichOneof("content") == "data":
                payload = json_format.MessageToDict(part.data)
        self.received.append(payload)
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        result = struct_pb2.Value()
        json_format.ParseDict({"skill": payload.get("skill"), **self.extra}, result)
        await updater.add_artifact([Part(data=result)], name="result")
        if self.finish:
            await updater.complete()
        else:
            await updater.start_work()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return None


def a2a_app(executor: AgentExecutor, base_url: str) -> Starlette:
    card = AgentCard(
        name="fake",
        description="fake agent",
        version="1",
        # Deliberately advertise a different host: the client must pin the origin.
        supported_interfaces=[
            AgentInterface(
                url="http://evil.internal/", protocol_binding="JSONRPC", protocol_version="1.0"
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
    )
    handler = DefaultRequestHandler(
        agent_executor=executor, task_store=InMemoryTaskStore(), agent_card=card
    )
    app = Starlette()
    app.routes.extend([*create_agent_card_routes(card), *create_jsonrpc_routes(handler, "/")])
    return app


# --------------------------------------------------------------------------- scripted LLM
class ScriptedLlm(BaseLlm):
    """Plays back a list of steps: a function-call dict, final text, or an exception to raise."""

    model: str = "scripted"
    script: list[Any] = []
    requests: list[LlmRequest] = []

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request)
        step = self.script.pop(0) if self.script else "done"
        if isinstance(step, Exception):
            raise step
        if isinstance(step, dict):
            part = types.Part(
                function_call=types.FunctionCall(name=step["name"], args=step["args"])
            )
        else:
            part = types.Part(text=step)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


# --------------------------------------------------------------------------- auth fakes
class FakeFirebase:
    TOKENS = {
        "admin-token": {
            "uid": "u-admin",
            "merchant_id": "m1",
            "role": "admin",
            "email": "admin@m1.test",
        },
        "viewer-token": {"uid": "u-viewer", "merchant_id": "m1", "role": "viewer"},
        "analyst-token": {"uid": "u-analyst", "merchant_id": "m1", "role": "analyst"},
        "other-tenant-token": {"uid": "u-x", "merchant_id": "m2", "role": "admin"},
        "no-claims-token": {"uid": "u-y"},
    }

    def verify(self, token: str) -> dict[str, Any]:
        if token not in self.TOKENS:
            raise AuthError("bad token")
        return self.TOKENS[token]


class FakeClients:
    def __init__(self) -> None:
        self.records = {
            "agent1": {
                "secret_hash": hash_client_secret("s3cret", PEPPER),
                "merchant_id": "m1",
                "kind": "shopper_agent",
                "ucp_profile": "https://agent.example/profile.json",
            },
            "partner1": {
                "secret_hash": hash_client_secret("s3cret", PEPPER),
                "merchant_id": "m1",
                "kind": "b2b_partner",
            },
            "disabled1": {
                "secret_hash": hash_client_secret("s3cret", PEPPER),
                "merchant_id": "m1",
                "kind": "shopper_agent",
                "disabled": True,
            },
        }

    async def get(self, client_id: str) -> dict[str, Any] | None:
        return self.records.get(client_id)


class FakeOidc:
    def verify(self, token: str, audience: str) -> dict[str, Any]:
        if token != "push-token" or audience != "https://acs.example.com/v1/intake/gmail-push":
            raise AuthError("bad")
        return {"email": "gmail-push@proj.iam.gserviceaccount.com", "email_verified": True}


# --------------------------------------------------------------------------- fixtures
RuleHandler = Callable[[dict[str, Any]], httpx.Response]


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="local",
        public_base_url=BASE_URL,
        api_key_pepper=PEPPER,
        gmail_push_audience="https://acs.example.com/v1/intake/gmail-push",
        gmail_push_service_account="gmail-push@proj.iam.gserviceaccount.com",
        gmail_merchant_id="m1",
        gmail_mailbox="orders@m1.test",
        rate_limit_per_minute=600,
    )


@pytest.fixture
def harness(settings: Settings):
    class Harness:
        pass

    h = Harness()
    h.settings = settings
    h.store = MemoryStore({"m1": {"storefront_domains": ["shop.example.com"]}})
    h.publisher = MemoryPoPublisher()
    h.rule_calls: list[dict[str, Any]] = []
    h.rule_response = {
        "decision": "APPROVE",
        "reasons": ["damaged within 30 days"],
        "matched_rule_ids": ["r1"],
        "rule_set_version": "v7",
        "max_refund_minor": 2000,
    }
    h.rule_status = 200

    def rules(request: httpx.Request) -> httpx.Response:
        h.rule_calls.append(json.loads(request.content))
        return httpx.Response(h.rule_status, json=h.rule_response)

    h.auditor_exec = EchoSkillExecutor({"score": 72}, finish=False)
    h.compiler_exec = EchoSkillExecutor({"draft_id": "draft-123", "rules": ["size(x) > 0"]})
    auditor_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a2a_app(h.auditor_exec, "http://auditor")),
        base_url="http://auditor",
    )
    compiler_http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a2a_app(h.compiler_exec, "http://compiler")),
        base_url="http://compiler",
    )
    rules_http = httpx.AsyncClient(transport=httpx.MockTransport(rules), base_url="http://rules")

    h.ops = Operations(
        settings=settings,
        store=h.store,
        publisher=h.publisher,
        rule_engine=RuleEngineClient(rules_http),
        auditor=A2AAgentClient("auditor", "http://auditor", auditor_http),
        policy_compiler=A2AAgentClient("policy_compiler", "http://compiler", compiler_http),
        resolve_dns=False,
    )
    bind_operations(h.ops)

    h.llm = ScriptedLlm(script=[], requests=[])
    session_service = InMemorySessionService()
    console_agent = build_orchestrator_agent("scripted")
    console_agent.model = h.llm
    shopper_agent = build_shopper_agent("scripted")
    shopper_agent.model = h.llm
    h.session_service = session_service
    container = Container(
        settings=settings,
        ops=h.ops,
        authenticator=Authenticator(
            settings, firebase=FakeFirebase(), clients=FakeClients(), oidc=FakeOidc()
        ),
        limiter=RateLimiter(settings.rate_limit_per_minute),
        session_service=session_service,
        console_runner=Runner(
            app_name=settings.app_name, agent=console_agent, session_service=session_service
        ),
        shopper_runner=Runner(
            app_name=f"{settings.app_name}_shopper",
            agent=shopper_agent,
            session_service=session_service,
            auto_create_session=True,
        ),
        closeables=[auditor_http, compiler_http, rules_http],
    )
    app = create_app(settings, container_factory=lambda _: container)
    with TestClient(app, base_url=BASE_URL) as client:
        h.client = client
        yield h


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


ADMIN = bearer("admin-token")
VIEWER = bearer("viewer-token")
ANALYST = bearer("analyst-token")
OTHER_TENANT = bearer("other-tenant-token")
AGENT = bearer("acsk_agent1.s3cret")
PARTNER = bearer("acsk_partner1.s3cret")

PDF_BYTES = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer\n%%EOF\n"

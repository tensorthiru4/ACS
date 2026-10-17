"""Fixtures: the compiler app behind httpx ASGI transport, a scripted Gemini, and an
A2A client that makes the same SDK calls as the orchestrator's A2AAgentClient."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.types import (
    GetTaskRequest,
    Message,
    Part,
    Role,
    SendMessageConfiguration,
    SendMessageRequest,
    TaskState,
)
from google.protobuf import json_format, struct_pb2

from acs_policy_compiler.config import Settings
from acs_policy_compiler.main import create_app
from acs_policy_compiler.repository import MemoryPolicyRepository


class ScriptedGemini:
    """Plays back JSON outputs (dicts) or raises exceptions, recording every request."""

    def __init__(self) -> None:
        self.script: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._generate))

    async def _generate(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append({"model": model, "contents": contents, "config": config})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(text=step if isinstance(step, str) else json.dumps(step))


class A2AHarness:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._client = None

    async def _get(self):
        if self._client is None:
            card = await A2ACardResolver(self._http, "http://compiler").get_agent_card()
            for interface in card.supported_interfaces:
                interface.url = "http://compiler/"
            self._client = ClientFactory(
                ClientConfig(httpx_client=self._http, streaming=False)
            ).create(card)
        return self._client

    async def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        data = struct_pb2.Value()
        json_format.ParseDict(payload, data)
        request = SendMessageRequest(
            message=Message(
                message_id=uuid.uuid4().hex,
                role=Role.ROLE_USER,
                parts=[Part(data=data, media_type="application/json")],
            ),
            configuration=SendMessageConfiguration(
                accepted_output_modes=["application/json", "text/plain"]
            ),
        )
        client = await self._get()
        last = None
        async for response in client.send_message(request):
            last = response
        task = last.task
        return self._view(task)

    async def get_task(self, task_id: str) -> dict[str, Any]:
        client = await self._get()
        return self._view(await client.get_task(GetTaskRequest(id=task_id)))

    @staticmethod
    def _view(task: Any) -> dict[str, Any]:
        data, text = [], []
        parts = [p for a in task.artifacts for p in a.parts]
        if task.status.HasField("message"):
            parts.extend(task.status.message.parts)
        for part in parts:
            if part.WhichOneof("content") == "data":
                data.append(json_format.MessageToDict(part.data))
            elif part.WhichOneof("content") == "text":
                text.append(part.text)
        return {
            "state": TaskState.Name(task.status.state),
            "task_id": task.id,
            "data": data,
            "text": text,
        }


@pytest.fixture
def gemini() -> ScriptedGemini:
    return ScriptedGemini()


@pytest.fixture
def repo() -> MemoryPolicyRepository:
    return MemoryPolicyRepository()


@pytest.fixture
async def a2a(gemini, repo):
    app = create_app(
        Settings(env="local", self_url="http://compiler"), genai_client=gemini, repository=repo
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://compiler"
    ) as http:
        yield A2AHarness(http)

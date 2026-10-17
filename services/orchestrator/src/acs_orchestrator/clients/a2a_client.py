"""Authenticated A2A (v1.0) client used for orchestrator -> agent delegation.

Diagram edges served by this client:
  * "A2A: Audit Store"     -> Agent-Readiness Auditor
  * "A2A: Compile Policy"  -> Policy-as-Code Compiler

Requests are a single structured DataPart (`{"skill": ..., ...}`) so the remote
agent receives typed input rather than prose that could be re-interpreted.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from a2a.client import A2ACardResolver, ClientConfig, ClientFactory
from a2a.client.client import Client
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

logger = logging.getLogger(__name__)

TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}


class A2ACallError(RuntimeError):
    pass


@dataclass
class A2AResult:
    state: str
    task_id: str | None = None
    context_id: str | None = None
    data: list[dict[str, Any]] = field(default_factory=list)
    text: list[str] = field(default_factory=list)

    @property
    def done(self) -> bool:
        return self.state in {TaskState.Name(s) for s in TERMINAL_STATES} or self.state == "MESSAGE"

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "task_id": self.task_id,
            "context_id": self.context_id,
            "data": self.data,
            "text": self.text,
        }


def _collect_parts(parts: Any, result: A2AResult) -> None:
    for part in parts:
        which = part.WhichOneof("content")
        if which == "data":
            value = json_format.MessageToDict(part.data)
            result.data.append(value if isinstance(value, dict) else {"value": value})
        elif which == "text":
            result.text.append(part.text)


def _pin_origin(url: str, base_url: str) -> str:
    """Keep the path the agent card advertises but force the configured origin.

    The card is fetched from the remote service; we never let it redirect our
    traffic (and our ID token) to a different host.
    """
    advertised = urlsplit(url)
    pinned = urlsplit(base_url)
    return urlunsplit((pinned.scheme, pinned.netloc, advertised.path or "/", "", ""))


class A2AAgentClient:
    def __init__(self, name: str, base_url: str, http: httpx.AsyncClient) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._client: Client | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> Client:
        async with self._lock:
            if self._client is None:
                if not self._base_url:
                    raise A2ACallError(f"{self.name} URL is not configured")
                card = await A2ACardResolver(self._http, self._base_url).get_agent_card()
                for interface in card.supported_interfaces:
                    interface.url = _pin_origin(interface.url, self._base_url)
                factory = ClientFactory(ClientConfig(httpx_client=self._http, streaming=False))
                self._client = factory.create(card)
            return self._client

    async def send(
        self,
        payload: dict[str, Any],
        *,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> A2AResult:
        data = struct_pb2.Value()
        json_format.ParseDict(payload, data)
        message = Message(
            message_id=uuid.uuid4().hex,
            role=Role.ROLE_USER,
            parts=[Part(data=data, media_type="application/json")],
        )
        if context_id:
            message.context_id = context_id
        if metadata:
            message.metadata.update(metadata)
        request = SendMessageRequest(
            message=message,
            configuration=SendMessageConfiguration(
                accepted_output_modes=["application/json", "text/plain"],
                return_immediately=not wait,
            ),
        )
        try:
            client = await self._get_client()
            result: A2AResult | None = None
            async for response in client.send_message(request):
                result = self._to_result(response)
        except A2ACallError:
            raise
        except Exception as exc:
            # Drop the cached client so a redeployed agent card is re-read next time.
            self._client = None
            logger.warning("A2A call to %s failed: %s", self.name, type(exc).__name__)
            raise A2ACallError(f"{self.name} is unavailable") from exc
        if result is None:
            raise A2ACallError(f"{self.name} returned no response")
        return result

    async def get_task(self, task_id: str) -> A2AResult:
        try:
            client = await self._get_client()
            task = await client.get_task(GetTaskRequest(id=task_id))
        except Exception as exc:
            self._client = None
            raise A2ACallError(f"{self.name} task lookup failed") from exc
        result = A2AResult(
            state=TaskState.Name(task.status.state), task_id=task.id, context_id=task.context_id
        )
        for artifact in task.artifacts:
            _collect_parts(artifact.parts, result)
        if task.status.HasField("message"):
            _collect_parts(task.status.message.parts, result)
        return result

    @staticmethod
    def _to_result(response: Any) -> A2AResult:
        if response.HasField("task"):
            task = response.task
            result = A2AResult(
                state=TaskState.Name(task.status.state),
                task_id=task.id,
                context_id=task.context_id,
            )
            for artifact in task.artifacts:
                _collect_parts(artifact.parts, result)
            if task.status.HasField("message"):
                _collect_parts(task.status.message.parts, result)
            return result
        if response.HasField("message"):
            result = A2AResult(state="MESSAGE", context_id=response.message.context_id or None)
            _collect_parts(response.message.parts, result)
            return result
        raise A2ACallError("unsupported A2A response")

    async def aclose(self) -> None:
        # The underlying httpx client is shared and owned by the app container.
        self._client = None

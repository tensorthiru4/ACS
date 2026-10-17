"""A2A (v1.0, JSON-RPC) server plumbing for agents the orchestrator delegates to.

Requests carry one structured DataPart (`{"skill": ..., ...}`) and results are
DataPart artifacts, so no prose is ever re-interpreted between agents.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks.task_store import TaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    Message,
    Part,
)
from google.protobuf import json_format, struct_pb2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.types import ASGIApp, Receive, Scope, Send

from .logging_setup import audit
from .service_auth import CallerAllowList, CallerRejected
from .web import RequestIdMiddleware

logger = logging.getLogger(__name__)

PUBLIC_PATHS = frozenset({"/health"})


def data_payload(message: Message | None) -> dict[str, Any]:
    """The first DataPart of a message as a dict ({} when there is none)."""
    if message is None:
        return {}
    for part in message.parts:
        if part.WhichOneof("content") == "data":
            value = json_format.MessageToDict(part.data)
            return value if isinstance(value, dict) else {}
    return {}


def data_part(value: dict[str, Any]) -> Part:
    data = struct_pb2.Value()
    # Round-trip through JSON so datetimes and other non-JSON types are rejected early.
    json_format.ParseDict(json.loads(json.dumps(value)), data)
    return Part(data=data, media_type="application/json")


def text_part(text: str) -> Part:
    return Part(text=text)


def agent_card(*, name: str, description: str, url: str, skills: list[AgentSkill]) -> AgentCard:
    return AgentCard(
        name=name,
        description=description,
        version="0.1.0",
        provider=AgentProvider(organization="Autonomous Commerce Suite", url=url),
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=skills,
    )


class CallerCheckMiddleware:
    """Rejects any request whose caller is not allow-listed (except /health and routes
    that do their own check)."""

    def __init__(
        self, app: ASGIApp, *, allow: CallerAllowList, self_checked: tuple[str, ...]
    ) -> None:
        self.app = app
        self._allow = allow
        self._self_checked = self_checked

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path in PUBLIC_PATHS or path.startswith(self._self_checked):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            await self._allow.caller(request.headers.get("authorization"))
        except CallerRejected as exc:
            audit("a2a.caller", principal="unknown", outcome="rejected", detail=str(exc))
            await JSONResponse({"error": "forbidden"}, status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


async def _health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_a2a_app(
    *,
    card: AgentCard,
    executor: AgentExecutor,
    task_store: TaskStore,
    allow: CallerAllowList,
    extra_routes: list[Route] | None = None,
    self_checked_prefixes: tuple[str, ...] = (),
) -> Starlette:
    """`self_checked_prefixes`: extra routes under these prefixes run their own caller check."""
    handler = DefaultRequestHandler(agent_executor=executor, task_store=task_store, agent_card=card)
    app = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            *(extra_routes or []),
            *create_agent_card_routes(card, cache_control="private, max-age=300"),
            *create_jsonrpc_routes(handler, "/"),
        ]
    )
    app.add_middleware(CallerCheckMiddleware, allow=allow, self_checked=self_checked_prefixes)
    app.add_middleware(RequestIdMiddleware)
    return app

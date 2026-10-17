"""Inbound A2A (v1.0, JSON-RPC) endpoint for AI shopper agents - diagram edge "UCP, A2A".

Routes (mounted on the main app):
  GET  /a2a/.well-known/agent-card.json   public agent card (no secrets)
  POST /a2a/                              JSON-RPC; requires an ACS shopper-agent credential

Authentication happens in `A2AAuthMiddleware` before the a2a-sdk handler runs, and
the verified principal becomes the A2A `ServerCallContext.user`, which ADK uses as
the session user id (so callers can never read each other's sessions or tasks).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from a2a.auth.user import User
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.runners import Runner
from starlette.requests import Request
from starlette.routing import BaseRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import Settings
from ..security.auth import Authenticator
from ..security.principals import AuthError, Capability, Principal, PrincipalKind
from ..security.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

A2A_PREFIX = "/a2a"
_PRINCIPAL_SCOPE_KEY = "acs.principal"
_CREDENTIAL_HEADERS = {
    "authorization",
    "x-forwarded-authorization",
    "cookie",
    "x-apigateway-api-userinfo",
}


class _PrincipalUser(User):
    def __init__(self, principal: Principal) -> None:
        self._principal = principal

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def user_name(self) -> str:
        return self._principal.adk_user_id


class _PrincipalContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        principal = request.scope.get(_PRINCIPAL_SCOPE_KEY)
        if principal is not None:
            context.user = _PrincipalUser(principal)
        # Never hand raw credentials to the executor / task store. Other headers stay:
        # the SDK reads A2A-Version and extension headers from them.
        headers = context.state.get("headers")
        if isinstance(headers, dict):
            context.state["headers"] = {
                k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS
            }
        return context


class A2AAuthMiddleware:
    """Authenticates every non-card request under /a2a before the SDK sees it."""

    def __init__(self, app: ASGIApp, *, authenticator: Authenticator, limiter: RateLimiter) -> None:
        self.app = app
        self._auth = authenticator
        self._limiter = limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(A2A_PREFIX):
            await self.app(scope, receive, send)
            return
        if path.endswith("/.well-known/agent-card.json") and scope.get("method") == "GET":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        try:
            principal = await self._auth.authenticate(
                request, frozenset({PrincipalKind.SHOPPER_AGENT})
            )
            if not principal.can(Capability.A2A_SHOPPER):
                raise AuthError("not allowed")
        except AuthError:
            await _json(
                send,
                401,
                {"error": "unauthorized"},
                [(b"www-authenticate", b'Bearer realm="acs-a2a"')],
            )
            return
        if not self._limiter.allow(principal.adk_user_id, cost=3):
            await _json(send, 429, {"error": "rate limit exceeded"})
            return
        scope[_PRINCIPAL_SCOPE_KEY] = principal
        await self.app(scope, receive, send)


async def _json(
    send: Send, status: int, body: dict[str, Any], headers: list[tuple[bytes, bytes]] | None = None
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), *(headers or [])],
        }
    )
    await send({"type": "http.response.body", "body": json.dumps(body).encode()})


def build_agent_card(settings: Settings) -> AgentCard:
    # No trailing slash: API Gateway path templates cannot end in "/". The gateway
    # forwards POST /a2a to this service's /a2a/ route.
    rpc_url = f"{settings.public_base_url}{A2A_PREFIX}"
    return AgentCard(
        name="ACS Shopper Gateway",
        description=(
            "Agent-facing service desk of an Autonomous Commerce Suite merchant. "
            "Returns deterministic refund decisions under the merchant's policy."
        ),
        version="0.1.0",
        provider=AgentProvider(
            organization="Autonomous Commerce Suite", url=settings.public_base_url
        ),
        supported_interfaces=[
            AgentInterface(url=rpc_url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        security_schemes={
            "acsClientKey": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(
                    scheme="bearer",
                    bearer_format="acsk_<client_id>.<secret>",
                    description="ACS-issued client credential bound to your UCP profile.",
                )
            )
        },
        security_requirements=[SecurityRequirement(schemes={"acsClientKey": {}})],
        skills=[
            AgentSkill(
                id="refund_decision",
                name="Refund decision",
                description=(
                    "Request a refund decision for an order. Provide order_id, amount in minor "
                    "units with ISO currency, and reason."
                ),
                tags=["refunds", "returns", "ucp"],
                examples=["Order 1001: refund 2599 USD, item arrived damaged 3 days ago."],
            )
        ],
    )


def build_a2a_routes(settings: Settings, shopper_runner: Runner) -> list[BaseRoute]:
    card = build_agent_card(settings)
    handler = DefaultRequestHandler(
        agent_executor=A2aAgentExecutor(runner=shopper_runner),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    return [
        *create_agent_card_routes(
            card,
            card_url=f"{A2A_PREFIX}/.well-known/agent-card.json",
            cache_control="public, max-age=300",
        ),
        *create_jsonrpc_routes(
            handler, f"{A2A_PREFIX}/", context_builder=_PrincipalContextBuilder()
        ),
    ]

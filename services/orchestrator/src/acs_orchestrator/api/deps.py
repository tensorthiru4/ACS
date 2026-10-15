"""FastAPI dependencies: container access, authentication and rate limiting."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Request

from ..container import Container
from ..security.principals import Principal, PrincipalKind


class RateLimitedError(Exception):
    pass


def get_container(request: Request) -> Container:
    return request.app.state.container


def authenticated(
    *kinds: PrincipalKind, cost: float = 1.0
) -> Callable[[Request], Awaitable[Principal]]:
    allowed = frozenset(kinds)

    async def dependency(request: Request) -> Principal:
        container = get_container(request)
        principal = await container.authenticator.authenticate(request, allowed)
        if not container.limiter.allow(principal.adk_user_id, cost):
            raise RateLimitedError
        request.state.principal = principal
        return principal

    return dependency


MerchantUser = authenticated(PrincipalKind.MERCHANT_USER)
MerchantChat = authenticated(PrincipalKind.MERCHANT_USER, cost=3.0)
ShopperAgent = authenticated(PrincipalKind.SHOPPER_AGENT)
IntakeSubmitter = authenticated(PrincipalKind.MERCHANT_USER, PrincipalKind.B2B_PARTNER, cost=2.0)
B2BPartner = authenticated(PrincipalKind.B2B_PARTNER, cost=2.0)

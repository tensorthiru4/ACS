"""Binding between ADK tool invocations and the authenticated principal / operations."""

from __future__ import annotations

from typing import Any

from ..operations import Operations
from ..security.principals import AuthError, Principal, PrincipalKind

PRINCIPAL_STATE_KEY = "acs:principal"

_ops: Operations | None = None


def bind_operations(ops: Operations) -> None:
    global _ops
    _ops = ops


def operations() -> Operations:
    if _ops is None:
        raise RuntimeError("operations not bound")
    return _ops


def principal_from_context(ctx: Any) -> Principal:
    """Resolve the caller for a tool call.

    The principal is written into session state by the API layer (never by the
    model) and must match the ADK user id the runner was invoked with, which is
    itself derived from verified credentials. A2A sessions created by the A2A
    executor have no state, so for them the user id alone is authoritative and
    only machine (shopper agent) principals are accepted.
    """
    user_id = ctx.user_id
    raw = ctx.state.get(PRINCIPAL_STATE_KEY)
    if raw:
        principal = Principal.model_validate(raw)
        if principal.adk_user_id != user_id:
            raise AuthError("principal does not match session owner")
        return principal
    merchant_id, _, rest = user_id.partition(":")
    kind, _, subject = rest.partition(":")
    if kind != PrincipalKind.SHOPPER_AGENT.value or not merchant_id or not subject:
        raise AuthError("no principal bound to this session")
    return Principal(kind=PrincipalKind.SHOPPER_AGENT, subject=subject, merchant_id=merchant_id)

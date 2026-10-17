"""ADK callbacks that enforce policy around every model and tool call."""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse
from google.adk.tools import BaseTool, ToolContext

from ..logging_setup import audit
from ..security.principals import AuthError, Capability
from ..security.redaction import redact, redact_text
from .context import principal_from_context

logger = logging.getLogger(__name__)

# Every tool the model can call, mapped to the capability it requires.
TOOL_CAPABILITIES: dict[str, Capability] = {
    "start_store_audit": Capability.AUDIT_RUN,
    "get_store_audit": Capability.AUDIT_READ,
    "compile_policy_draft": Capability.POLICY_COMPILE,
    "request_refund_decision": Capability.REFUND_DECIDE,
    "get_po_intake_status": Capability.INTAKE_READ,
}

MAX_TOOL_CALLS_PER_TURN = 8
# temp: state lives for one invocation only and is never persisted.
_CALLS_KEY = "temp:acs_tool_calls"


def enforce_tool_policy(
    tool: BaseTool, args: dict[str, Any], tool_context: ToolContext
) -> dict[str, Any] | None:
    """before_tool_callback: allow-list + RBAC + per-turn call budget.

    Returning a dict short-circuits the tool and hands that dict to the model.
    """
    capability = TOOL_CAPABILITIES.get(tool.name)
    if capability is None:
        logger.warning("blocked unknown tool %s", tool.name)
        return {"status": "error", "error": "Tool not available."}
    try:
        principal = principal_from_context(tool_context)
    except AuthError:
        return {"status": "error", "error": "Session is not bound to an authenticated user."}

    calls = int(tool_context.state.get(_CALLS_KEY, 0)) + 1
    tool_context.state[_CALLS_KEY] = calls
    if calls > MAX_TOOL_CALLS_PER_TURN:
        return {"status": "error", "error": "Tool call budget for this turn exhausted."}

    if not principal.can(capability):
        audit("tool.denied", principal=principal.adk_user_id, outcome="denied", tool=tool.name)
        return {"status": "error", "error": "You are not permitted to perform this action."}
    audit(
        "tool.call",
        principal=principal.adk_user_id,
        outcome="allowed",
        tool=tool.name,
        args=redact(args),
    )
    return None


def redact_model_output(
    callback_context: CallbackContext, llm_response: LlmResponse
) -> LlmResponse | None:
    """after_model_callback: strip secrets / PII patterns from model text."""
    content = llm_response.content
    if not content or not content.parts:
        return None
    changed = False
    for part in content.parts:
        if part.text:
            cleaned = redact_text(part.text)
            if cleaned != part.text:
                part.text = cleaned
                changed = True
    return llm_response if changed else None

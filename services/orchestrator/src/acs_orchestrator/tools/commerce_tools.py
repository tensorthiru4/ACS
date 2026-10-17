"""ADK function tools exposed to the orchestrator LLM.

Each tool is a thin adapter over `Operations`: the principal comes from the
session (never from model arguments), inputs are re-validated with the same
Pydantic models as the REST API, and downstream output is wrapped as untrusted
data before it is returned to the model.

Rule deployment is intentionally absent: it requires an explicit admin action
through `POST /v1/policies/{draft_id}/deploy`.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.tools import ToolContext
from pydantic import ValidationError

from ..agents.context import operations, principal_from_context
from ..clients.a2a_client import A2ACallError
from ..models import AuditRequest, PolicyCompileRequest, RefundRequest
from ..operations import ConflictError, NotFoundError
from ..security.guardrails import wrap_untrusted
from ..security.principals import AuthError, ForbiddenError
from ..security.ssrf import UnsafeUrlError

logger = logging.getLogger(__name__)


def _error(message: str) -> dict[str, Any]:
    return {"status": "error", "error": message}


async def _guard(coro: Any) -> dict[str, Any]:
    try:
        return await coro
    except (AuthError, ForbiddenError):
        return _error("You are not permitted to perform this action.")
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return _error(f"Invalid input for: {', '.join(fields)}")
    except UnsafeUrlError as exc:
        return _error(f"URL rejected: {exc}")
    except NotFoundError:
        return _error("Not found.")
    except ConflictError as exc:
        return _error(str(exc))
    except A2ACallError as exc:
        return _error(str(exc))


async def start_store_audit(
    store_url: str, journeys: list[str] | None, tool_context: ToolContext
) -> dict[str, Any]:
    """Start an agent-readiness audit of one of the merchant's storefronts.

    The Agent-Readiness Auditor simulates AI-shopper journeys against the storefront's
    UCP endpoints and writes readiness scores and suggested fixes to BigQuery.
    Audits run asynchronously; use get_store_audit with the returned task_id.

    Args:
        store_url: https URL of a storefront registered to this merchant.
        journeys: Optional subset of ["discovery", "product_detail", "cart", "checkout",
            "returns"]. Omit to run the default set.
    """

    async def run() -> dict[str, Any]:
        principal = principal_from_context(tool_context)
        req = AuditRequest(store_url=store_url, **({"journeys": journeys} if journeys else {}))
        view = await operations().start_audit(principal, req)
        return {"status": "ok", "task_id": view.task_id, "state": view.state}

    return await _guard(run())


async def get_store_audit(task_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Get the status and results of a previously started storefront audit.

    Args:
        task_id: The task_id returned by start_store_audit.
    """

    async def run() -> dict[str, Any]:
        principal = principal_from_context(tool_context)
        view = await operations().get_audit(principal, task_id)
        return {
            "status": "ok",
            "task_id": view.task_id,
            "state": view.state,
            "result": wrap_untrusted("agent_readiness_auditor", view.result)
            if view.result
            else None,
        }

    return await _guard(run())


async def compile_policy_draft(
    policy_name: str, policy_text: str, target_rule_set: str, tool_context: ToolContext
) -> dict[str, Any]:
    """Compile a natural-language business policy into a DRAFT rule set.

    The Policy-as-Code Compiler returns JSON-Schema-validated CEL rules. Drafts are
    never deployed by this tool; an admin must approve deployment in the console.

    Args:
        policy_name: Short identifier, lowercase letters, digits, '-' or '_'.
        policy_text: The policy wording to compile (20-20000 characters).
        target_rule_set: One of "refunds", "order_terms", "pricing".
    """

    async def run() -> dict[str, Any]:
        principal = principal_from_context(tool_context)
        req = PolicyCompileRequest(
            policy_name=policy_name, policy_text=policy_text, target_rule_set=target_rule_set
        )
        view = await operations().compile_policy(principal, req)
        return {
            "status": "ok",
            "task_id": view.task_id,
            "state": view.state,
            "result": wrap_untrusted("policy_compiler", view.result) if view.result else None,
            "next_step": "An admin must review and deploy the draft from the console.",
        }

    return await _guard(run())


async def request_refund_decision(
    order_id: str,
    amount_minor: int,
    currency: str,
    reason: str,
    days_since_delivery: int | None,
    item_condition: str | None,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Ask the deterministic rule engine for a refund decision.

    The decision (APPROVE / DENY / ESCALATE) is made by the merchant's CEL rules;
    report it verbatim and never alter or second-guess it.

    Args:
        order_id: Order identifier.
        amount_minor: Requested refund in minor units (e.g. cents).
        currency: ISO-4217 code such as "USD".
        reason: One of damaged, not_as_described, not_received, late_delivery,
            wrong_item, changed_mind, other.
        days_since_delivery: Days since delivery, if known.
        item_condition: One of unopened, opened, used, damaged, missing, if known.
    """

    async def run() -> dict[str, Any]:
        principal = principal_from_context(tool_context)
        req = RefundRequest.model_validate(
            {
                "order_id": order_id,
                "amount": {"amount_minor": amount_minor, "currency": currency},
                "reason": reason,
                "days_since_delivery": days_since_delivery,
                "item_condition": item_condition,
            }
        )
        decision = await operations().decide_refund(principal, req)
        return {"status": "ok", **decision.model_dump(mode="json")}

    return await _guard(run())


async def get_po_intake_status(intake_id: str, tool_context: ToolContext) -> dict[str, Any]:
    """Look up the processing status of a purchase order submitted to the B2B inbox.

    Args:
        intake_id: The intake_id returned when the PO was uploaded or e-mailed.
    """

    async def run() -> dict[str, Any]:
        principal = principal_from_context(tool_context)
        record = await operations().get_intake(principal, intake_id)
        return {"status": "ok", **{k: str(v) if v is not None else None for k, v in record.items()}}

    return await _guard(run())

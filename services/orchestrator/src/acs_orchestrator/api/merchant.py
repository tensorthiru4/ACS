"""Deterministic (non-LLM) merchant endpoints for audits, policies and refunds."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request

from ..models import (
    A2ATaskView,
    AuditRequest,
    PolicyCompileRequest,
    PolicyDeployRequest,
    RefundDecision,
    RefundRequest,
)
from ..security.principals import Capability, Principal, require
from . import live_progress
from .deps import MerchantUser, get_container

router = APIRouter(prefix="/v1", tags=["merchant"])

IdempotencyKey = Annotated[
    str | None, Header(alias="Idempotency-Key", max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
]


@router.post("/audits", status_code=202)
async def start_audit(
    body: AuditRequest, request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> A2ATaskView:
    return await get_container(request).ops.start_audit(principal, body)


@router.get("/audits/{task_id}")
async def get_audit(
    task_id: str, request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> A2ATaskView:
    return await get_container(request).ops.get_audit(principal, task_id)


@router.post("/policies/compile", response_model=A2ATaskView)
async def compile_policy(
    body: PolicyCompileRequest,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
) -> Any:
    """Compile a draft. With `X-Progress-Id` its progress can be polled while it runs."""
    ops = get_container(request).ops
    require(principal, Capability.POLICY_COMPILE)

    async def work(emit: live_progress.Emit) -> A2ATaskView:
        emit({"type": "status", "text": "Sending the policy to the compiler agent"})
        return await ops.compile_policy(principal, body)

    return await live_progress.run_tracked(
        ops.store, principal.adk_user_id, live_progress.progress_id(request), work
    )


@router.post("/policies/{draft_id}/deploy")
async def deploy_policy(
    draft_id: str,
    body: PolicyDeployRequest,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
) -> A2ATaskView:
    return await get_container(request).ops.deploy_policy(principal, draft_id, body)


@router.post("/refunds/decide")
async def decide_refund(
    body: RefundRequest,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
    idempotency_key: IdempotencyKey = None,
) -> RefundDecision:
    return await get_container(request).ops.decide_refund(principal, body, idempotency_key)

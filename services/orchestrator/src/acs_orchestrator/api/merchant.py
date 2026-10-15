"""Deterministic (non-LLM) merchant endpoints for audits, policies and refunds."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from ..models import (
    A2ATaskView,
    AuditRequest,
    PolicyCompileRequest,
    PolicyDeployRequest,
    RefundDecision,
    RefundRequest,
)
from ..security.principals import Principal
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


@router.post("/policies/compile")
async def compile_policy(
    body: PolicyCompileRequest,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
) -> A2ATaskView:
    return await get_container(request).ops.compile_policy(principal, body)


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

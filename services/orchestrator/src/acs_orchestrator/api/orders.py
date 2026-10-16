"""Draft sales orders from the B2B inbox: review, release to the ERP, or reject.

Releasing is a human decision (merchant admin) and is deliberately not an LLM tool.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request

from ..models import DraftRejectRequest, DraftReleaseRequest
from ..security.principals import Principal
from .deps import MerchantUser, get_container

router = APIRouter(prefix="/v1/orders/drafts", tags=["orders"])


@router.get("")
async def list_drafts(
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
    status: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[dict[str, Any]]:
    return await get_container(request).ops.list_draft_orders(principal, status, limit)


@router.get("/{intake_id}")
async def get_draft(
    intake_id: str, request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> dict[str, Any]:
    return await get_container(request).ops.get_draft_order(principal, intake_id)


@router.post("/{intake_id}/release")
async def release_draft(
    intake_id: str,
    body: DraftReleaseRequest,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
) -> dict[str, Any]:
    return await get_container(request).ops.release_draft_order(principal, intake_id, body)


@router.post("/{intake_id}/reject")
async def reject_draft(
    intake_id: str,
    body: DraftRejectRequest,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantUser)],
) -> dict[str, Any]:
    return await get_container(request).ops.reject_draft_order(principal, intake_id, body)

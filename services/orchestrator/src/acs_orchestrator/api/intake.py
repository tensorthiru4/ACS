"""B2B inbox channel: PDF / voice uploads, partner webhooks and Gmail push."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile

from ..logging_setup import audit
from ..models import IntakeReceipt, IntakeSource, WebhookPO
from ..security.files import RejectedFileError
from ..security.principals import AuthError, Principal
from .deps import B2BPartner, IntakeSubmitter, MerchantUser, get_container

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/intake", tags=["intake"])


@router.post("/uploads", status_code=202)
async def upload_po(
    request: Request,
    principal: Annotated[Principal, Depends(IntakeSubmitter)],
    file: Annotated[UploadFile, File()],
    customer_ref: Annotated[str | None, Form(max_length=128)] = None,
) -> IntakeReceipt:
    c = get_container(request)
    data = await file.read(c.settings.max_po_bytes + 1)
    if len(data) > c.settings.max_po_bytes:
        raise RejectedFileError(f"file exceeds {c.settings.max_po_bytes} bytes")
    return await c.ops.submit_po(
        principal,
        data,
        filename=file.filename,
        source=IntakeSource.UPLOAD,
        customer_ref=customer_ref,
    )


@router.post("/webhook", status_code=202)
async def webhook_po(
    body: WebhookPO, request: Request, principal: Annotated[Principal, Depends(B2BPartner)]
) -> IntakeReceipt:
    return await get_container(request).ops.submit_po_base64(
        principal,
        body.content_base64,
        filename=body.filename,
        source=IntakeSource.WEBHOOK,
        customer_ref=body.customer_ref or principal.subject,
    )


@router.post("/gmail-push", status_code=204)
async def gmail_push(request: Request) -> Response:
    """Pub/Sub push endpoint for Gmail `users.watch` notifications.

    Returns 204 for anything handled (including ignored notifications) so Pub/Sub
    does not redeliver; transient Gmail errors return 503 to trigger a retry.
    """
    c = get_container(request)
    try:
        principal = await c.authenticator.authenticate_gmail_push(request)
    except AuthError:
        audit("intake.gmail_push", principal="unknown", outcome="unauthenticated")
        raise
    try:
        envelope: Any = await request.json()
    except ValueError:
        return Response(status_code=204)
    if not isinstance(envelope, dict):
        return Response(status_code=204)
    try:
        queued = await c.ops.process_gmail_push(principal, envelope)
    except Exception as exc:
        from googleapiclient.errors import HttpError

        if isinstance(exc, HttpError) and exc.resp.status in (429, 500, 502, 503, 504):
            raise HTTPException(status_code=503, detail="gmail temporarily unavailable") from exc
        if isinstance(exc, HttpError) and exc.resp.status == 404:
            # historyId too old: Gmail requires a full resync; reset the cursor.
            logger.warning("gmail push: history expired, cursor will reset on next push")
            await c.ops.store.set_gmail_history(c.settings.gmail_mailbox.lower(), "")
            return Response(status_code=204)
        raise
    audit("intake.gmail_push", principal=principal.adk_user_id, outcome="ok", queued=queued)
    return Response(status_code=204)


@router.get("/{intake_id}")
async def get_intake(
    intake_id: str, request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> dict[str, Any]:
    return await get_container(request).ops.get_intake(principal, intake_id)

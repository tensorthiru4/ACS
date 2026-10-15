"""Merchant Console chat API - the ADK orchestrator agent behind Firebase Auth."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.genai import types

from ..agents.context import PRINCIPAL_STATE_KEY
from ..logging_setup import audit
from ..models import ChatMessage, ChatReply, SessionInfo
from ..security.guardrails import GuardrailViolation, injection_signals, normalize_user_text
from ..security.principals import Capability, Principal, require
from .deps import MerchantChat, MerchantUser, get_container

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/console", tags=["console"])

MAX_SESSIONS_LISTED = 50


@router.post("/sessions", status_code=201)
async def create_session(
    request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> dict[str, str]:
    require(principal, Capability.CONSOLE_CHAT)
    c = get_container(request)
    session = await c.session_service.create_session(
        app_name=c.settings.app_name,
        user_id=principal.adk_user_id,
        state={PRINCIPAL_STATE_KEY: principal.model_dump(mode="json")},
    )
    audit(
        "console.session.create",
        principal=principal.adk_user_id,
        outcome="ok",
        session_id=session.id,
    )
    return {"session_id": session.id}


@router.get("/sessions")
async def list_sessions(
    request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> list[SessionInfo]:
    require(principal, Capability.CONSOLE_CHAT)
    c = get_container(request)
    resp = await c.session_service.list_sessions(
        app_name=c.settings.app_name, user_id=principal.adk_user_id
    )
    sessions = sorted(resp.sessions, key=lambda s: s.last_update_time, reverse=True)
    return [
        SessionInfo(session_id=s.id, last_update_time=s.last_update_time)
        for s in sessions[:MAX_SESSIONS_LISTED]
    ]


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str, request: Request, principal: Annotated[Principal, Depends(MerchantUser)]
) -> Response:
    require(principal, Capability.CONSOLE_CHAT)
    c = get_container(request)
    # Scoped by user id: another user's session id is simply not found.
    await c.session_service.delete_session(
        app_name=c.settings.app_name, user_id=principal.adk_user_id, session_id=session_id
    )
    return Response(status_code=204)


@router.post("/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: ChatMessage,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantChat)],
) -> ChatReply:
    require(principal, Capability.CONSOLE_CHAT)
    c = get_container(request)
    text = normalize_user_text(body.text, c.settings.max_chat_chars)
    signals = injection_signals(text)
    if signals:
        audit(
            "console.prompt_injection",
            principal=principal.adk_user_id,
            outcome="blocked",
            session_id=session_id,
            signals=signals,
        )
        raise GuardrailViolation(
            "Message blocked by prompt-injection screening. Rephrase without instructions "
            "aimed at the assistant's configuration."
        )

    session = await c.session_service.get_session(
        app_name=c.settings.app_name, user_id=principal.adk_user_id, session_id=session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    reply: list[str] = []
    tool_calls: list[str] = []
    message = types.Content(role="user", parts=[types.Part(text=text)])
    try:
        async with asyncio.timeout(c.settings.downstream_timeout_s + 30):
            async for event in c.console_runner.run_async(
                user_id=principal.adk_user_id,
                session_id=session_id,
                new_message=message,
                # Refresh the bound principal every turn so role changes apply immediately.
                state_delta={PRINCIPAL_STATE_KEY: principal.model_dump(mode="json")},
            ):
                tool_calls.extend(fc.name for fc in event.get_function_calls() if fc.name)
                if event.is_final_response() and event.content and event.content.parts:
                    reply.extend(p.text for p in event.content.parts if p.text and not p.thought)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="the assistant timed out") from exc

    return ChatReply(
        session_id=session_id,
        reply="\n".join(reply).strip() or "(no response)",
        tool_calls=tool_calls,
    )

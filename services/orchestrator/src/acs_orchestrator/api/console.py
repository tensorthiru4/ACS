"""Merchant Console chat API - the ADK orchestrator agent behind Firebase Auth."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.genai import types

from ..agents import progress
from ..agents.context import PRINCIPAL_STATE_KEY
from ..container import Container
from ..logging_setup import audit
from ..models import ChatMessage, ChatReply, SessionInfo
from ..security.guardrails import GuardrailViolation, injection_signals, normalize_user_text
from ..security.principals import Capability, Principal, require
from . import live_progress
from .deps import MerchantChat, MerchantPoll, MerchantUser, get_container
from .live_progress import Emit

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/console", tags=["console"])

MAX_SESSIONS_LISTED = 50
TITLE_STATE_KEY = "acs:title"
MAX_TITLE_CHARS = 48


def conversation_title(text: str) -> str:
    """A short name for a conversation, taken from its first message."""
    words = " ".join(text.split())
    if len(words) <= MAX_TITLE_CHARS:
        return words
    cut = words[:MAX_TITLE_CHARS].rsplit(" ", 1)[0] or words[:MAX_TITLE_CHARS]
    return cut.rstrip(" ,.;:-") + "…"


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
        SessionInfo(
            session_id=s.id,
            last_update_time=s.last_update_time,
            title=s.state.get(TITLE_STATE_KEY) or None,
        )
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


@router.post("/sessions/{session_id}/messages", response_model=ChatReply)
async def send_message(
    session_id: str,
    body: ChatMessage,
    request: Request,
    principal: Annotated[Principal, Depends(MerchantChat)],
) -> Any:
    """One chat turn. With `X-Progress-Id` its progress can be polled while it runs."""
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
    # Named once, from the first message, so the list can tell conversations apart.
    title = None if session.state.get(TITLE_STATE_KEY) else conversation_title(text)

    return await live_progress.run_tracked(
        c.ops.store,
        principal.adk_user_id,
        live_progress.progress_id(request),
        lambda emit: _run_turn(c, principal, session_id, text, emit, title),
    )


@router.get("/progress/{progress_id}")
async def get_progress(
    progress_id: str, request: Request, principal: Annotated[Principal, Depends(MerchantPoll)]
) -> dict[str, Any]:
    """Progress of the caller's own slow call that sent `X-Progress-Id` (see live_progress)."""
    c = get_container(request)
    return await live_progress.read_progress(c.ops.store, principal.adk_user_id, progress_id)


def _tool_failed(response: Any) -> bool:
    return isinstance(response, dict) and response.get("status") == "error"


async def _run_turn(
    c: Container,
    principal: Principal,
    session_id: str,
    text: str,
    emit: Emit,
    title: str | None = None,
) -> ChatReply:
    reply: list[str] = []
    tool_calls: list[str] = []
    # Refresh the bound principal every turn so role changes apply immediately.
    state_delta: dict[str, Any] = {PRINCIPAL_STATE_KEY: principal.model_dump(mode="json")}
    if title:
        state_delta[TITLE_STATE_KEY] = title
    message = types.Content(role="user", parts=[types.Part(text=text)])
    emit({"type": "status", "text": "Thinking"})
    try:
        with progress.reporting_to(emit):
            async with asyncio.timeout(c.settings.downstream_timeout_s + 30):
                async for event in c.console_runner.run_async(
                    user_id=principal.adk_user_id,
                    session_id=session_id,
                    new_message=message,
                    state_delta=state_delta,
                ):
                    for fc in event.get_function_calls():
                        if fc.name:
                            tool_calls.append(fc.name)
                            emit({"type": "tool_call", "name": fc.name})
                    responses = event.get_function_responses()
                    for fr in responses:
                        emit(
                            {
                                "type": "tool_result",
                                "name": fr.name,
                                "ok": not _tool_failed(fr.response),
                            }
                        )
                    if responses:
                        emit({"type": "status", "text": "Reading the results"})
                    if event.is_final_response() and event.content and event.content.parts:
                        reply.extend(
                            p.text for p in event.content.parts if p.text and not p.thought
                        )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="the assistant timed out") from exc

    return ChatReply(
        session_id=session_id,
        reply="\n".join(reply).strip() or "(no response)",
        tool_calls=tool_calls,
    )

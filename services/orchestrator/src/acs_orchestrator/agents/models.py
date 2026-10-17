"""Model chain for the orchestrator's ADK agents: Gemini first, Gemma when Gemini is down.

The Gemini key is on the AI Studio free tier (about 20 requests/day per model), so a
busy day runs it out of quota. `FallbackLlm` then sends the same turn - history, tool
declarations and tool results - to Gemma on the same key (a separate quota), and skips
Gemini for a cool-down so later turns do not first wait on a model that is known to be
down.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import AsyncGenerator
from typing import Any

import httpx
from google.adk.models import BaseLlm, LlmRequest, LlmResponse
from google.genai import errors as genai_errors
from pydantic import PrivateAttr

from . import progress

logger = logging.getLogger(__name__)

FALLBACK_CODES = {429, 500, 502, 503, 504}
MAX_COOLDOWN_S = 600.0
OVERLOAD_COOLDOWN_S = 60.0


def _retry_delay_s(exc: genai_errors.APIError) -> float | None:
    details = exc.details if isinstance(exc.details, dict) else {}
    error = details.get("error", details)
    for item in error.get("details", []) if isinstance(error, dict) else []:
        match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(item.get("retryDelay") or ""))
        if match:
            return float(match.group(1))
    return None


class FallbackLlm(BaseLlm):
    """Runs each model call on `primary`; on 429/5xx or a timeout, on `fallback` instead."""

    primary: BaseLlm
    fallback: BaseLlm
    _down_until: float = PrivateAttr(default=0.0)

    def __init__(self, **data: Any) -> None:
        # ADK detects capabilities from the model name, so report the primary's.
        data.setdefault("model", data["primary"].model)
        super().__init__(**data)

    def _note_failure(self, exc: Exception) -> str:
        if isinstance(exc, genai_errors.APIError) and exc.code == 429:
            delay = _retry_delay_s(exc) or OVERLOAD_COOLDOWN_S
            reason = "is out of quota"
        else:
            delay = OVERLOAD_COOLDOWN_S
            reason = "is overloaded"
        self._down_until = time.monotonic() + min(delay, MAX_COOLDOWN_S)
        return reason

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        if time.monotonic() >= self._down_until:
            yielded = False
            try:
                async for response in self.primary.generate_content_async(llm_request, stream):
                    yielded = True
                    yield response
                return
            except (genai_errors.APIError, httpx.TimeoutException) as exc:
                code = getattr(exc, "code", None)
                if yielded or (code is not None and code not in FALLBACK_CODES):
                    raise
                reason = self._note_failure(exc)
                logger.warning(
                    "%s %s (%s); falling back to %s",
                    self.primary.model,
                    reason,
                    code or type(exc).__name__,
                    self.fallback.model,
                )
                primary_error: Exception = exc
        else:
            reason = "is unavailable"
            primary_error = genai_errors.ServerError(
                503, {"error": {"code": 503, "status": "UNAVAILABLE"}}
            )
        progress.report(
            {
                "type": "model",
                "model": self.fallback.model,
                "text": f"Gemini {reason}; answering with {self.fallback.model}",
            }
        )
        try:
            async for response in self.fallback.generate_content_async(llm_request, stream):
                yield response
        except (genai_errors.APIError, httpx.HTTPError) as exc:
            logger.warning("fallback model failed too: %s", exc)
            # Surface the Gemini error: the API maps it to "busy or out of quota" (503).
            raise primary_error from exc

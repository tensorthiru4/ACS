"""Client for the Deterministic Rule Engine (Cloud Run, CEL) - diagram edge "Refund Decisions".

Refund outcomes are decided only by CEL rules; the LLM can ask for a decision but
can never produce one. Any failure fails *safe* to ESCALATE (human review).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from pydantic import ValidationError

from ..models import RuleEngineResult

logger = logging.getLogger(__name__)


class RuleEngineError(RuntimeError):
    pass


class RuleEngineClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def evaluate(
        self, *, merchant_id: str, rule_set: str, facts: dict[str, Any], request_id: str
    ) -> RuleEngineResult:
        body = {
            "merchant_id": merchant_id,
            "rule_set": rule_set,
            "facts": facts,
            "request_id": request_id,
        }
        try:
            response = await self._http.post("/v1/evaluate", json=body)
        except httpx.HTTPError as exc:
            raise RuleEngineError("rule engine unreachable") from exc
        if response.status_code != 200:
            logger.warning("rule engine returned HTTP %s", response.status_code)
            raise RuleEngineError(f"rule engine returned HTTP {response.status_code}")
        try:
            return RuleEngineResult.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise RuleEngineError("rule engine returned an invalid payload") from exc

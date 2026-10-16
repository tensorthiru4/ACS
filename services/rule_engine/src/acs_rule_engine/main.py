"""Deterministic Rule Engine - `POST /v1/evaluate` (contract: docs/contracts/README.md#3).

Callers: the orchestrator ("Refund Decisions"), the messy-order agent ("Validates
Terms") and the policy compiler. Decisions come only from CEL rules; any failure
on the caller side fails safe to ESCALATE.
"""

# No `from __future__ import annotations`: FastAPI must resolve the local Depends() closures.

import logging
import os
from typing import Annotated, Any, Literal

from acs_common.logging_setup import audit, configure_logging
from acs_common.rules import Decision, FactValidationError
from acs_common.service_auth import CallerAllowList
from acs_common.web import RequestIdMiddleware, caller, error, install_error_handlers
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Settings, get_settings
from .engine import FirestoreRuleSets, MemoryRuleSets, RuleEngine

logger = logging.getLogger(__name__)


class EvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    merchant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    rule_set: Literal["refunds", "order_terms", "pricing"]
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    facts: dict[str, Any]

    @field_validator("facts")
    @classmethod
    def _bounded(cls, facts: dict[str, Any]) -> dict[str, Any]:
        if len(facts) > 64:
            raise ValueError("too many facts")
        return facts


class EvaluateResponse(BaseModel):
    decision: Decision
    reasons: list[str]
    matched_rule_ids: list[str]
    rule_set_version: str
    max_refund_minor: int | None = None


def build_engine(settings: Settings) -> RuleEngine:
    if settings.project_id:
        from google.cloud import firestore

        db = firestore.AsyncClient(
            project=settings.project_id, database=settings.firestore_database
        )
        return RuleEngine(FirestoreRuleSets(db), cache_ttl_s=settings.cache_ttl_s)
    logger.warning("ACS_PROJECT_ID not set: serving rule sets from memory")
    source = (
        MemoryRuleSets.from_file(settings.seed_rules_file)
        if settings.seed_rules_file
        else MemoryRuleSets()
    )
    return RuleEngine(source, cache_ttl_s=settings.cache_ttl_s)


def create_app(settings: Settings | None = None, engine: RuleEngine | None = None) -> FastAPI:
    settings = settings or get_settings()
    engine = engine or build_engine(settings)
    allow = CallerAllowList(settings.allowed_callers, enabled=not settings.is_local)
    app = FastAPI(
        title="ACS Rule Engine",
        version="0.1.0",
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(FactValidationError)
    async def _facts(_: Request, exc: FactValidationError) -> JSONResponse:
        return error(422, str(exc))

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/evaluate")
    async def evaluate(
        body: EvaluateRequest,
        service: Annotated[str, Depends(caller(allow, "rules.evaluate"))],
    ) -> EvaluateResponse:
        result, version = await engine.evaluate(body.merchant_id, body.rule_set, body.facts)
        audit(
            "rules.evaluate",
            principal=service,
            outcome=result.decision.value,
            merchant_id=body.merchant_id,
            rule_set=body.rule_set,
            rule_set_version=version,
            request_id=body.request_id,
            matched_rule_ids=result.matched_rule_ids,
            errored_rule_ids=result.errored_rule_ids,
        )
        return EvaluateResponse(
            decision=result.decision,
            reasons=result.reasons[:50],
            matched_rule_ids=result.matched_rule_ids[:200],
            rule_set_version=version,
            max_refund_minor=result.max_refund_minor,
        )

    return app


def run() -> None:
    import uvicorn

    configure_logging("acs-rule-engine")
    uvicorn.run(
        create_app(),
        host="0.0.0.0",  # noqa: S104 - container port, fronted by Cloud Run
        port=int(os.environ.get("PORT", "8080")),
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=8,
        log_config=None,
    )


if __name__ == "__main__":
    run()

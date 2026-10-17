"""Messy-Order-to-ERP agent - FastAPI app on Cloud Run.

Routes:
  POST /pubsub/po-intake                 Pub/Sub push ("Push Consume"), caller acs-pubsub-push@
  POST /v1/drafts/{intake_id}/release    human approval relayed by the orchestrator
  POST /v1/drafts/{intake_id}/reject     human rejection relayed by the orchestrator
  GET  /health

Push responses: 204 acknowledges (done, duplicate or permanently failed); 503 asks
Pub/Sub to redeliver with backoff (transient error or another delivery holds the lease).
"""

# No `from __future__ import annotations`: FastAPI must resolve the local Depends() closures.
import logging
import os
from typing import Annotated, Any

from acs_common.logging_setup import audit, configure_logging
from acs_common.service_auth import CallerAllowList, service_client
from acs_common.web import RequestIdMiddleware, caller, error, install_error_handlers
from fastapi import Depends, FastAPI, Path, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .adapters import (
    GcsRawPoStore,
    MemoryErpLauncher,
    MemoryRawPoStore,
    TermsChecker,
    WorkflowsErpLauncher,
)
from .catalog import FirestoreCatalog, MemoryCatalog, SkuMatcher
from .config import Settings, get_settings
from .extraction import Extractor
from .models import PermanentError, parse_push
from .pipeline import IntakeBusy, InvalidOverride, Pipeline
from .repository import DraftNotFound, DraftStateError, FirestoreRepository, MemoryRepository

logger = logging.getLogger(__name__)

_INTAKE_ID = r"^[0-9a-f]{40}$"


class LineOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_no: int = Field(ge=1, le=200)
    sku: str = Field(min_length=1, max_length=64)


class ReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    merchant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    approved_by: str = Field(min_length=1, max_length=256)
    line_overrides: list[LineOverride] = Field(default_factory=list, max_length=200)
    customer_ref: str | None = Field(default=None, max_length=128)


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    merchant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    rejected_by: str = Field(min_length=1, max_length=256)
    reason: str = Field(default="", max_length=500)


def draft_summary(draft: dict[str, Any]) -> dict[str, Any]:
    erp = draft.get("erp") or {}
    return {
        "intake_id": draft["intake_id"],
        "status": draft["status"],
        "total_minor": draft.get("total_minor"),
        "currency": draft.get("currency"),
        "erp_execution": erp.get("execution"),
    }


def build_pipeline(settings: Settings) -> Pipeline:
    from acs_common.gemini import genai_client

    client = genai_client()
    if settings.project_id:
        from google.cloud import firestore

        db = firestore.AsyncClient(
            project=settings.project_id, database=settings.firestore_database
        )
        repo: Any = FirestoreRepository(db)
        catalog: Any = FirestoreCatalog(db)
        raw_store: Any = GcsRawPoStore(settings.raw_po_bucket)
        erp: Any = (
            WorkflowsErpLauncher(settings.project_id, settings.region, settings.erp_workflow)
            if settings.erp_workflow
            else None
        )
    else:
        logger.warning("ACS_PROJECT_ID not set: using in-memory stores")
        repo, catalog, raw_store, erp = (
            MemoryRepository(),
            MemoryCatalog(),
            MemoryRawPoStore(),
            MemoryErpLauncher(),
        )
    rules_http = (
        service_client(
            settings.rule_engine_url,
            timeout_s=settings.downstream_timeout_s,
            authenticate=not settings.is_local,
            user_agent="acs-messy-order/0.1",
        )
        if settings.rule_engine_url
        else None
    )
    return Pipeline(
        settings=settings,
        repo=repo,
        raw_store=raw_store,
        extractor=Extractor(client, settings.gemini_model),
        matcher=SkuMatcher(
            catalog,
            client=client,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            max_distance=settings.sku_max_distance,
            min_margin=settings.sku_min_margin,
        ),
        terms=TermsChecker(rules_http),
        erp=erp,
    )


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or get_settings()
    pipeline = pipeline or build_pipeline(settings)
    enabled = not settings.is_local
    push_caller = caller(CallerAllowList(settings.push_callers, enabled=enabled), "po.push")
    orchestrator = caller(
        CallerAllowList(settings.orchestrator_callers, enabled=enabled), "po.review"
    )
    app = FastAPI(
        title="ACS Messy-Order-to-ERP",
        version="0.1.0",
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.is_local else None,
    )
    app.state.pipeline = pipeline
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)

    @app.exception_handler(DraftNotFound)
    async def _missing(_: Request, __: DraftNotFound) -> JSONResponse:
        return error(404, "draft not found")

    @app.exception_handler(DraftStateError)
    async def _state(_: Request, exc: DraftStateError) -> JSONResponse:
        return error(409, str(exc))

    @app.exception_handler(InvalidOverride)
    async def _override(_: Request, exc: InvalidOverride) -> JSONResponse:
        return error(422, str(exc))

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/pubsub/po-intake", status_code=204)
    async def po_intake(request: Request, _: Annotated[str, Depends(push_caller)]) -> Response:
        try:
            envelope = await request.json()
            event = parse_push(envelope, max_bytes=settings.max_po_bytes)
        except (ValueError, PermanentError) as exc:
            # Never retry a malformed message: it cannot succeed.
            logger.warning("dropping malformed PO event: %s", exc)
            audit("po.intake", principal="pubsub", outcome="malformed", detail=str(exc)[:200])
            return Response(status_code=204)
        try:
            outcome = await pipeline.handle(event)
        except IntakeBusy:
            return Response(status_code=503, headers={"Retry-After": "60"})
        except Exception as exc:
            logger.warning(
                "PO %s attempt %d failed: %s",
                event.attributes.intake_id,
                event.delivery_attempt,
                type(exc).__name__,
            )
            return Response(status_code=503)
        logger.info("PO %s -> %s", event.attributes.intake_id, outcome)
        return Response(status_code=204)

    @app.post("/v1/drafts/{intake_id}/release")
    async def release(
        body: ReleaseRequest,
        _: Annotated[str, Depends(orchestrator)],
        intake_id: Annotated[str, Path(pattern=_INTAKE_ID)],
    ) -> dict[str, Any]:
        draft = await pipeline.release(
            intake_id,
            merchant_id=body.merchant_id,
            approved_by=body.approved_by,
            line_overrides={o.line_no: o.sku for o in body.line_overrides},
            customer_ref=body.customer_ref,
        )
        return draft_summary(draft)

    @app.post("/v1/drafts/{intake_id}/reject")
    async def reject(
        body: RejectRequest,
        _: Annotated[str, Depends(orchestrator)],
        intake_id: Annotated[str, Path(pattern=_INTAKE_ID)],
    ) -> dict[str, Any]:
        draft = await pipeline.reject(
            intake_id, merchant_id=body.merchant_id, rejected_by=body.rejected_by,
            reason=body.reason,
        )  # fmt: skip
        return draft_summary(draft)

    return app


def run() -> None:
    import uvicorn

    configure_logging("acs-messy-order")
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

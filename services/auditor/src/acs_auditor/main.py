"""Agent-Readiness Auditor - A2A v1.0 JSON-RPC server (contract: docs/contracts/README.md#1).

Routes:
  GET  /.well-known/agent-card.json, POST /     A2A (caller: orchestrator)
  POST /internal/audits/{task_id}/run           Cloud Tasks run (caller: this service's SA)
  GET  /health
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentSkill
from acs_common.a2a_app import agent_card, build_a2a_app
from acs_common.logging_setup import audit, configure_logging
from acs_common.service_auth import CallerAllowList, CallerRejected
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .advisor import FixAdvisor
from .browser import StorefrontBrowser
from .config import Settings, get_settings
from .executor import AuditExecutor
from .infra import BigQuerySink, CloudTasksDispatcher, InProcessDispatcher, MemorySink
from .runner import AuditRunner

logger = logging.getLogger(__name__)
_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
RUN_PATH = "/internal/audits/{task_id}/run"

SKILL = AgentSkill(
    id="audit_store",
    name="Audit store",
    description=(
        "Simulate AI-shopper journeys (discovery, product detail, cart, checkout, returns) "
        "against a storefront and score its agent readiness, with suggested fixes."
    ),
    tags=["audit", "ucp", "agent-readiness"],
)


def create_app(
    settings: Settings | None = None,
    *,
    task_store: Any = None,
    collector: Any = None,
    advisor: FixAdvisor | None = None,
    sink: Any = None,
    dispatcher: Any = None,
    resolve_dns: bool = True,
) -> Starlette:
    settings = settings or get_settings()
    if task_store is None:
        if settings.project_id:
            from acs_common.a2a_store import FirestoreTaskStore
            from google.cloud import firestore

            db = firestore.AsyncClient(
                project=settings.project_id, database=settings.firestore_database
            )
            task_store = FirestoreTaskStore(db, "auditor")
        else:
            task_store = InMemoryTaskStore()
    if sink is None:
        sink = (
            BigQuerySink(settings.project_id, settings.bigquery_dataset)
            if settings.project_id
            else MemorySink()
        )
    if advisor is None:
        from acs_common.gemini import genai_client

        advisor = FixAdvisor(genai_client() if os.environ.get("GOOGLE_API_KEY") else None,
                             settings.gemini_model)  # fmt: skip
    collector = collector or StorefrontBrowser(
        page_timeout_s=settings.page_timeout_s, budget_s=settings.audit_budget_s
    )
    if dispatcher is None:
        if settings.tasks_queue:
            dispatcher = CloudTasksDispatcher(
                project_id=settings.project_id,
                region=settings.region,
                queue=settings.tasks_queue,
                target_url=settings.self_url,
                service_account=settings.tasks_service_account,
            )
        else:
            if not settings.is_local:
                raise RuntimeError("ACS_TASKS_QUEUE is required outside local development")
            dispatcher = InProcessDispatcher()
    runner = AuditRunner(store=task_store, collector=collector, advisor=advisor, sink=sink)
    if isinstance(dispatcher, InProcessDispatcher):
        dispatcher.runner = runner

    run_callers = CallerAllowList(settings.runner_callers, enabled=not settings.is_local)

    async def run_audit(request: Request) -> JSONResponse:
        try:
            await run_callers.caller(request.headers.get("authorization"))
        except CallerRejected as exc:
            audit("audit.run.caller", principal="unknown", outcome="rejected", detail=str(exc))
            return JSONResponse({"error": "forbidden"}, status_code=403)
        task_id = request.path_params["task_id"]
        if not _TASK_ID.match(task_id):
            return JSONResponse({"error": "not found"}, status_code=404)
        outcome = await runner.run(task_id)
        if outcome == "missing":
            # Not persisted yet (or unknown): let Cloud Tasks retry with backoff.
            return JSONResponse({"outcome": outcome}, status_code=503)
        return JSONResponse({"outcome": outcome})

    card = agent_card(
        name="ACS Agent-Readiness Auditor",
        description="Scores how well a storefront serves AI shopping agents and suggests fixes.",
        url=settings.self_url.rstrip("/") + "/",
        skills=[SKILL],
    )
    app = build_a2a_app(
        card=card,
        executor=AuditExecutor(dispatcher, resolve_dns=resolve_dns),
        task_store=task_store,
        allow=CallerAllowList(settings.allowed_callers, enabled=not settings.is_local),
        extra_routes=[Route(RUN_PATH, run_audit, methods=["POST"])],
        self_checked_prefixes=("/internal/",),
    )
    app.state.runner = runner
    return app


def run() -> None:
    import uvicorn

    configure_logging("acs-auditor")
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

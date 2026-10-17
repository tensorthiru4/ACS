"""Policy-as-Code Compiler - A2A v1.0 JSON-RPC server (contract: docs/contracts/README.md#2)."""

from __future__ import annotations

import logging
import os
from typing import Any

from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentSkill
from acs_common.a2a_app import agent_card, build_a2a_app
from acs_common.logging_setup import configure_logging
from acs_common.service_auth import CallerAllowList
from starlette.applications import Starlette

from .compiler import PolicyCompiler
from .config import Settings, get_settings
from .executor import CompilerExecutor
from .repository import FirestorePolicyRepository, MemoryPolicyRepository

logger = logging.getLogger(__name__)

SKILLS = [
    AgentSkill(
        id="compile_policy",
        name="Compile policy",
        description=(
            "Compile natural-language merchant policy into a draft CEL rule set "
            "(refunds, order_terms or pricing) with test results. Never deploys."
        ),
        tags=["policy", "cel", "rules"],
    ),
    AgentSkill(
        id="deploy_rules",
        name="Deploy rules",
        description="Deploy an approved draft with optimistic concurrency on the rule-set version.",
        tags=["policy", "deploy"],
    ),
]


def create_app(
    settings: Settings | None = None,
    *,
    genai_client: Any = None,
    repository: Any = None,
    task_store: Any = None,
) -> Starlette:
    settings = settings or get_settings()
    db = None
    if settings.project_id and (repository is None or task_store is None):
        from google.cloud import firestore

        db = firestore.AsyncClient(
            project=settings.project_id, database=settings.firestore_database
        )
    if repository is None:
        repository = (
            FirestorePolicyRepository(
                db,
                draft_ttl_days=settings.draft_ttl_days,
                distinct_approver=settings.require_distinct_approver,
            )
            if db is not None
            else MemoryPolicyRepository(distinct_approver=settings.require_distinct_approver)
        )
    if task_store is None:
        if db is not None:
            from acs_common.a2a_store import FirestoreTaskStore

            task_store = FirestoreTaskStore(db, "policy_compiler")
        else:
            task_store = InMemoryTaskStore()
    if genai_client is None:
        from acs_common.gemini import genai_client as make_client

        genai_client = make_client()

    compiler = PolicyCompiler(
        client=genai_client,
        model=settings.gemini_model,
        repository=repository,
        max_attempts=settings.max_attempts,
    )
    card = agent_card(
        name="ACS Policy-as-Code Compiler",
        description="Turns merchant policy text into validated, versioned CEL rule sets.",
        url=settings.self_url.rstrip("/") + "/",
        skills=SKILLS,
    )
    return build_a2a_app(
        card=card,
        executor=CompilerExecutor(compiler),
        task_store=task_store,
        allow=CallerAllowList(settings.allowed_callers, enabled=not settings.is_local),
    )


def run() -> None:
    import uvicorn

    configure_logging("acs-policy-compiler")
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

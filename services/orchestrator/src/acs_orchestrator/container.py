"""Wires settings into concrete clients. Tests build a Container with fakes instead."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

import httpx
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, InMemorySessionService
from google.genai import types

from .agents.context import bind_operations
from .agents.orchestrator_agent import build_orchestrator_agent, build_shopper_agent
from .clients.a2a_client import A2AAgentClient
from .clients.pubsub import MemoryPoPublisher, PubSubPoPublisher
from .clients.rule_engine import RuleEngineClient
from .clients.service_auth import service_client
from .clients.store import FirestoreStore, MemoryStore
from .config import Settings
from .operations import Operations
from .security.auth import (
    Authenticator,
    FirebaseAdminVerifier,
    FirestoreApiClientStore,
    GoogleOidcVerifier,
)
from .security.ratelimit import RateLimiter

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    ops: Operations
    authenticator: Authenticator
    limiter: RateLimiter
    session_service: BaseSessionService
    console_runner: Runner
    shopper_runner: Runner
    closeables: list[Any] = field(default_factory=list)

    async def aclose(self) -> None:
        for item in self.closeables:
            try:
                await item.aclose()
            except Exception:  # pragma: no cover - best effort on shutdown
                logger.exception("error while closing %r", item)


def gemini_model(name: str) -> Gemini:
    """Gemini with retries for transient overload (free-tier models often return 503).

    429 is left out on purpose: retrying a quota error only burns more of the quota.
    """
    return Gemini(
        model=name,
        retry_options=types.HttpRetryOptions(
            attempts=4, initial_delay=2.0, max_delay=16.0, http_status_codes=[500, 503, 504]
        ),
    )


def build_runners(settings: Settings, session_service: BaseSessionService) -> tuple[Runner, Runner]:
    console = Runner(
        app_name=settings.app_name,
        agent=build_orchestrator_agent(gemini_model(settings.gemini_model)),
        session_service=session_service,
    )
    shopper = Runner(
        app_name=f"{settings.app_name}_shopper",
        agent=build_shopper_agent(gemini_model(settings.gemini_model)),
        session_service=session_service,
        auto_create_session=True,
    )
    return console, shopper


def build_container(settings: Settings) -> Container:
    # Gemini through Google AI Studio (free tier); the key arrives via Secret Manager env.
    os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "FALSE")

    use_gcp = bool(settings.project_id)
    authenticate_downstream = not settings.is_local
    closeables: list[Any] = []

    def downstream(url: str) -> httpx.AsyncClient | None:
        if not url:
            return None
        client = service_client(
            url, timeout_s=settings.downstream_timeout_s, authenticate=authenticate_downstream
        )
        closeables.append(client)
        return client

    auditor_http = downstream(settings.auditor_url)
    compiler_http = downstream(settings.policy_compiler_url)
    rules_http = downstream(settings.rule_engine_url)

    if use_gcp:
        from google.cloud import firestore

        db = firestore.AsyncClient(
            project=settings.project_id, database=settings.firestore_database
        )
        from .sessions.firestore_session_service import FirestoreSessionService

        store: Any = FirestoreStore(db)
        session_service: BaseSessionService = FirestoreSessionService(
            db, ttl_days=settings.session_ttl_days
        )
        publisher: Any = PubSubPoPublisher(settings.project_id, settings.po_intake_topic)
        client_store: Any = FirestoreApiClientStore(db)
    else:
        logger.warning("ACS_PROJECT_ID not set: using in-memory store, sessions and publisher")
        store = MemoryStore()
        session_service = InMemorySessionService()
        publisher = MemoryPoPublisher()
        client_store = None

    gmail = None
    oauth_json = settings.gmail_oauth_json.get_secret_value()
    if oauth_json and settings.gmail_mailbox:
        from .clients.gmail import GmailReader

        gmail = GmailReader(
            oauth_json, settings.gmail_allowed_sender_domains, settings.max_po_bytes
        )

    ops = Operations(
        settings=settings,
        store=store,
        publisher=publisher,
        rule_engine=RuleEngineClient(rules_http) if rules_http else None,
        auditor=A2AAgentClient("auditor", settings.auditor_url, auditor_http)
        if auditor_http
        else None,
        policy_compiler=A2AAgentClient(
            "policy_compiler", settings.policy_compiler_url, compiler_http
        )
        if compiler_http
        else None,
        gmail=gmail,
    )
    bind_operations(ops)

    authenticator = Authenticator(
        settings,
        firebase=FirebaseAdminVerifier(
            settings.firebase_project_id, settings.firebase_check_revoked
        )
        if settings.firebase_project_id
        else None,
        clients=client_store,
        oidc=GoogleOidcVerifier(),
    )
    console_runner, shopper_runner = build_runners(settings, session_service)
    return Container(
        settings=settings,
        ops=ops,
        authenticator=authenticator,
        limiter=RateLimiter(settings.rate_limit_per_minute),
        session_service=session_service,
        console_runner=console_runner,
        shopper_runner=shopper_runner,
        closeables=closeables,
    )

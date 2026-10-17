"""ACS Orchestrator - FastAPI application served by uvicorn on Cloud Run."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.adk.runners import Runner

from .api import console, errors, intake, merchant, orders, ucp
from .api.a2a_server import A2AAuthMiddleware, build_a2a_routes
from .config import Settings, get_settings
from .container import Container, build_container
from .logging_setup import configure_logging, request_id_var
from .security.middleware import SecurityMiddleware

logger = logging.getLogger(__name__)


def _error(status: int, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": message, "request_id": request_id_var.get()}, status_code=status, headers=headers
    )


def _install_error_handlers(app: FastAPI) -> None:
    async def _handle(_: Request, exc: Exception) -> JSONResponse:
        failure = errors.describe(exc)
        return _error(failure.status, failure.message, failure.headers)

    for exc_type in errors.HANDLED:
        app.add_exception_handler(exc_type, _handle)


def create_app(
    settings: Settings | None = None,
    container_factory: Callable[[Settings], Container] = build_container,
) -> FastAPI:
    settings = settings or get_settings()
    state: dict[str, Container] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = container_factory(settings)
        state["container"] = container
        app.state.container = container
        logger.info("orchestrator started", extra={"fields": {"env": settings.env.value}})
        try:
            yield
        finally:
            await container.aclose()

    is_prod = not settings.is_local
    app = FastAPI(
        title="ACS Orchestrator",
        version="0.1.0",
        lifespan=lifespan,
        # No interactive docs in deployed environments (smaller attack surface).
        docs_url=None if is_prod else "/docs",
        redoc_url=None,
        openapi_url=None if is_prod else "/openapi.json",
    )
    _install_error_handlers(app)

    app.include_router(console.router)
    app.include_router(merchant.router)
    app.include_router(intake.router)
    app.include_router(orders.router)
    app.include_router(ucp.router)

    def shopper_runner() -> Runner:
        return state["container"].shopper_runner

    app.router.routes.extend(build_a2a_routes(settings, shopper_runner))

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # Middleware order: last added runs first.
    app.add_middleware(
        A2AAuthMiddleware,
        authenticator=_LazyAuthenticator(state),
        limiter=_LazyLimiter(state),
    )
    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Request-Id",
                "X-Progress-Id",
            ],
            allow_credentials=False,
            max_age=600,
        )
    app.add_middleware(SecurityMiddleware, max_body_bytes=settings.max_request_bytes)
    return app


class _LazyAuthenticator:
    """Defers to the container's authenticator, which exists only after startup."""

    def __init__(self, state: dict[str, Container]) -> None:
        self._state = state

    async def authenticate(self, request, allowed):  # type: ignore[no-untyped-def]
        return await self._state["container"].authenticator.authenticate(request, allowed)


class _LazyLimiter:
    def __init__(self, state: dict[str, Container]) -> None:
        self._state = state

    def allow(self, key: str, cost: float = 1.0) -> bool:
        return self._state["container"].limiter.allow(key, cost)


def run() -> None:
    import uvicorn

    settings = get_settings()
    configure_logging(settings.project_id)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",  # noqa: S104 - container port, fronted by Cloud Run
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
        date_header=False,
        timeout_graceful_shutdown=8,
        log_config=None,
    )


if __name__ == "__main__":
    run()

"""FastAPI plumbing shared by the agents: caller checks, request ids, error envelopes."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .logging_setup import audit, request_id_var
from .service_auth import CallerAllowList, CallerRejected

logger = logging.getLogger(__name__)
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message, "request_id": request_id_var.get()}, status_code=status)


class RequestIdMiddleware:
    """Binds a request id (Cloud Run trace id when present) for log correlation."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        trace = headers.get(b"x-cloud-trace-context", b"").decode("latin-1").split("/")[0]
        token = request_id_var.set(trace if _REQUEST_ID.match(trace) else uuid.uuid4().hex)
        try:
            await self.app(scope, receive, send)
        finally:
            request_id_var.reset(token)


def caller(allow: CallerAllowList, route: str) -> Callable[[Request], Awaitable[str]]:
    """FastAPI dependency returning the verified calling service account."""

    async def dependency(request: Request) -> str:
        try:
            return await allow.caller(request.headers.get("authorization"))
        except CallerRejected as exc:
            audit(f"{route}.caller", principal="unknown", outcome="rejected", detail=str(exc))
            raise

    return dependency


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(CallerRejected)
    async def _rejected(_: Request, __: CallerRejected) -> JSONResponse:
        return error(403, "forbidden")

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Echo field locations only, never the submitted values.
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return error(422, f"invalid request: {', '.join(fields)}")

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return error(500, "internal error")

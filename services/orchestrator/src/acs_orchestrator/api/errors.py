"""Exception -> HTTP status and client-safe message, for the JSON exception handlers."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from google.genai import errors as genai_errors

from ..clients.a2a_client import A2ACallError
from ..clients.order_desk import OrderDeskError, OrderDeskRejected
from ..operations import ConflictError, NotFoundError
from ..security.files import RejectedFileError
from ..security.guardrails import GuardrailViolation
from ..security.principals import AuthError, ForbiddenError
from ..security.ssrf import UnsafeUrlError
from .deps import RateLimitedError

logger = logging.getLogger(__name__)

MODEL_BUSY = "The AI model is busy or out of quota. Try again shortly."

HANDLED: tuple[type[Exception], ...] = (
    AuthError,
    ForbiddenError,
    RateLimitedError,
    RequestValidationError,
    GuardrailViolation,
    UnsafeUrlError,
    RejectedFileError,
    NotFoundError,
    ConflictError,
    A2ACallError,
    OrderDeskError,
    OrderDeskRejected,
    genai_errors.APIError,
    Exception,
)


@dataclass(frozen=True)
class Failure:
    status: int
    message: str
    headers: dict[str, str] | None = None


def describe(exc: Exception) -> Failure:
    if isinstance(exc, AuthError):
        logger.info("authentication failed: %s", exc)
        return Failure(401, "unauthorized", {"WWW-Authenticate": 'Bearer realm="acs"'})
    if isinstance(exc, ForbiddenError):
        logger.info("authorization failed: %s", exc)
        return Failure(403, "forbidden")
    if isinstance(exc, RateLimitedError):
        return Failure(429, "rate limit exceeded", {"Retry-After": "10"})
    if isinstance(exc, RequestValidationError):
        # Echo field locations only, never the submitted values.
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return Failure(422, f"invalid request: {', '.join(fields)}")
    if isinstance(exc, GuardrailViolation):
        return Failure(422, str(exc))
    if isinstance(exc, UnsafeUrlError):
        return Failure(422, f"URL rejected: {exc}")
    if isinstance(exc, RejectedFileError):
        return Failure(415, str(exc))
    if isinstance(exc, NotFoundError):
        return Failure(404, "not found")
    if isinstance(exc, ConflictError):
        return Failure(409, str(exc))
    if isinstance(exc, A2ACallError | OrderDeskError):
        return Failure(502, str(exc))
    if isinstance(exc, OrderDeskRejected):
        return Failure(exc.status, str(exc) if exc.status != 404 else "not found")
    if isinstance(exc, genai_errors.APIError):
        logger.warning("model call failed: %s %s", exc.code, exc.status)
        if exc.code in (429, 500, 503, 504):
            return Failure(503, MODEL_BUSY, {"Retry-After": "30"})
        return Failure(502, "AI model request failed")
    if isinstance(exc, HTTPException):
        return Failure(exc.status_code, str(exc.detail))
    if isinstance(exc, TimeoutError):
        return Failure(504, "the request timed out")
    logger.error("unhandled error", exc_info=exc)
    return Failure(500, "internal error")

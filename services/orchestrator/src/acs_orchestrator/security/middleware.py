"""Pure-ASGI middleware: request ids / trace correlation, body-size cap, security headers."""

from __future__ import annotations

import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..logging_setup import request_id_var, trace_var

_SECURITY_HEADERS = [
    (b"strict-transport-security", b"max-age=63072000; includeSubDomains; preload"),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-site"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cache-control", b"no-store"),
]

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        incoming_id = headers.get("x-request-id", "")
        request_id = incoming_id if _REQUEST_ID_RE.match(incoming_id) else uuid.uuid4().hex
        trace = headers.get("x-cloud-trace-context", "").split("/")[0]
        rid_token = request_id_var.set(request_id)
        trace_token = trace_var.set(trace if re.fullmatch(r"[0-9a-f]{32}", trace) else "")

        declared = headers.get("content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_body_bytes):
            await _plain(send, 413, b"request body too large", request_id)
            return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        async def secured_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                existing = {k.lower() for k, _ in message.get("headers", [])}
                extra = [(k, v) for k, v in _SECURITY_HEADERS if k not in existing]
                extra.append((b"x-request-id", request_id.encode()))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        try:
            await self.app(scope, limited_receive, secured_send)
        except _BodyTooLarge:
            if not started:
                await _plain(send, 413, b"request body too large", request_id)
        finally:
            request_id_var.reset(rid_token)
            trace_var.reset(trace_token)


class _BodyTooLarge(Exception):
    pass


async def _plain(send: Send, status: int, body: bytes, request_id: str) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"x-request-id", request_id.encode()),
                *_SECURITY_HEADERS,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})

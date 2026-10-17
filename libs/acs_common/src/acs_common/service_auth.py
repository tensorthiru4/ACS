"""Service-to-service authentication between ACS workloads.

Outbound: `service_client()` attaches a Google-signed ID token whose audience is the
callee's base URL (minted from the Cloud Run metadata server).

Inbound: every agent runs with `--no-allow-unauthenticated`, so Cloud Run IAM
(`roles/run.invoker`) is the gate. `CallerAllowList` adds defense in depth: it
checks *which* service account called a route, so a stray invoker grant (or a
caller allowed on one route but not another) is still refused.

Cloud Run verifies the token and then forwards it with the signature replaced by
`SIGNATURE_REMOVED_BY_GOOGLE`. That form is only trusted when running on Cloud
Run (`K_SERVICE` set); a token that still carries a signature is verified here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import time
from collections.abc import AsyncGenerator, Generator
from typing import Any

import httpx

STRIPPED_SIGNATURE = "SIGNATURE_REMOVED_BY_GOOGLE"


class CallerRejected(Exception):
    """The caller is not authenticated or not on the route's allow-list (HTTP 403)."""


class IdTokenAuth(httpx.Auth):
    """httpx auth flow that attaches a cached ID token for `audience`."""

    requires_request_body = False

    def __init__(self, audience: str, *, refresh_margin_s: int = 300) -> None:
        self._audience = audience
        self._token: str | None = None
        self._expires_at = 0.0
        self._margin = refresh_margin_s
        self._lock = asyncio.Lock()

    def _fetch(self) -> tuple[str, float]:
        import google.auth.transport.requests
        from google.oauth2 import id_token

        token = id_token.fetch_id_token(google.auth.transport.requests.Request(), self._audience)
        # Google ID tokens live for 1h; avoid decoding just to read exp.
        return token, time.time() + 3600

    async def _get(self) -> str:
        async with self._lock:
            if self._token is None or time.time() > self._expires_at - self._margin:
                self._token, self._expires_at = await asyncio.to_thread(self._fetch)
            return self._token

    def sync_auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        raise RuntimeError("IdTokenAuth is async-only")

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._get()}"
        response = yield request
        if response.status_code == 401:
            self._token = None
            request.headers["Authorization"] = f"Bearer {await self._get()}"
            yield request


def service_client(
    base_url: str, *, timeout_s: float, authenticate: bool, user_agent: str
) -> httpx.AsyncClient:
    """HTTP client for a private Cloud Run service (audience = its base URL)."""
    return httpx.AsyncClient(
        base_url=base_url,
        auth=IdTokenAuth(base_url) if authenticate else None,
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": user_agent},
    )


def _b64json(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    value = json.loads(base64.urlsafe_b64decode(padded))
    if not isinstance(value, dict):
        raise ValueError("JWT segment is not an object")
    return value


class CallerAllowList:
    """Checks the calling service account against an allow-list.

    `enabled=False` (local development) accepts every request and reports the
    caller as "local".
    """

    def __init__(self, allowed_emails: list[str], *, enabled: bool) -> None:
        self._allowed = {e.lower() for e in allowed_emails if e}
        self._enabled = enabled

    def _verify_signed(self, token: str) -> dict[str, Any]:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        # Audience was already enforced by Cloud Run; issuer, expiry and signature here.
        return id_token.verify_oauth2_token(token, ga_requests.Request(), audience=None)

    async def caller(self, authorization: str | None) -> str:
        if not self._enabled:
            return "local"
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or token.count(".") != 2 or len(token) > 4096:
            raise CallerRejected("missing bearer token")
        header_b64, payload_b64, signature = token.split(".")
        try:
            if signature == STRIPPED_SIGNATURE:
                if not os.environ.get("K_SERVICE"):
                    raise CallerRejected("stripped token outside Cloud Run")
                _b64json(header_b64)
                claims = _b64json(payload_b64)
                if float(claims.get("exp", 0)) < time.time() - 30:
                    raise CallerRejected("token expired")
            else:
                claims = await asyncio.to_thread(self._verify_signed, token)
        except CallerRejected:
            raise
        except (ValueError, binascii.Error, TypeError) as exc:
            raise CallerRejected("malformed token") from exc
        except Exception as exc:  # google-auth raises several error types
            raise CallerRejected("invalid token") from exc
        email = str(claims.get("email", "")).lower()
        if not claims.get("email_verified") or email not in self._allowed:
            raise CallerRejected("caller not allowed on this route")
        return email

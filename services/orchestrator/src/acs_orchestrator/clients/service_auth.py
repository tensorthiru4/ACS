"""Service-to-service authentication: Google-signed ID tokens for private Cloud Run services.

Every downstream agent is deployed with `--no-allow-unauthenticated` and grants
`roles/run.invoker` only to the orchestrator's service account, so the token
minted here (from the Cloud Run metadata server) is the only way in.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, Generator

import httpx


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
            # Token may have been revoked/rotated; refresh once.
            self._token = None
            request.headers["Authorization"] = f"Bearer {await self._get()}"
            yield request


def service_client(base_url: str, *, timeout_s: float, authenticate: bool) -> httpx.AsyncClient:
    """HTTP client for a private Cloud Run service (audience = its base URL)."""
    return httpx.AsyncClient(
        base_url=base_url,
        auth=IdTokenAuth(base_url) if authenticate else None,
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "acs-orchestrator/0.1"},
    )

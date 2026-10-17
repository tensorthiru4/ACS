"""Authentication for every channel in the architecture diagram.

Request path: client -> Firebase Hosting / API Gateway -> Cloud Run (IAM, invoker = gateway SA).

* Cloud Run itself is deployed with `--no-allow-unauthenticated`, so only the API
  Gateway service account and the Pub/Sub push service account can reach it.
* API Gateway replaces `Authorization` with its own Google-signed token and moves the
  caller's original header to `X-Forwarded-Authorization`. We never trust the
  gateway's decoded `X-Apigateway-Api-Userinfo` header; the original credential is
  re-verified here (defense in depth).

Credentials accepted:
* Merchant Console     -> Firebase ID token (custom claims `merchant_id`, `role`)
* AI shopper agents     -> ACS client credential `acsk_<client_id>.<secret>`, bound to a UCP profile
* B2B partner systems   -> ACS client credential (kind `b2b_partner`)
* Gmail push (Pub/Sub)  -> Google-signed OIDC token for a pinned service account
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from typing import Any, Protocol

from cachetools import TTLCache
from fastapi import Request

from ..config import Settings
from .principals import AuthError, Principal, PrincipalKind, Role

logger = logging.getLogger(__name__)

CLIENT_TOKEN_PREFIX = "acsk_"  # noqa: S105 - a prefix, not a secret
DEV_PRINCIPAL_HEADER = "x-acs-dev-principal"


def extract_bearer(request: Request) -> str:
    """Return the caller's original bearer credential."""
    header = request.headers.get("x-forwarded-authorization") or request.headers.get(
        "authorization", ""
    )
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("missing bearer credential")
    token = token.strip()
    if len(token) > 4096:
        raise AuthError("credential too large")
    return token


def hash_client_secret(secret: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), secret.encode(), hashlib.sha256).hexdigest()


class FirebaseTokenVerifier(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...


class ApiClientStore(Protocol):
    async def get(self, client_id: str) -> dict[str, Any] | None: ...


class OidcVerifier(Protocol):
    def verify(self, token: str, audience: str) -> dict[str, Any]: ...


class FirebaseAdminVerifier:
    """Verifies Firebase ID tokens (signature, expiry, audience, issuer, revocation)."""

    def __init__(self, project_id: str, check_revoked: bool) -> None:
        import firebase_admin
        from firebase_admin import auth as fb_auth

        self._auth = fb_auth
        try:
            self._app = firebase_admin.get_app("acs")
        except ValueError:
            self._app = firebase_admin.initialize_app(options={"projectId": project_id}, name="acs")
        self._check_revoked = check_revoked

    def verify(self, token: str) -> dict[str, Any]:
        try:
            return self._auth.verify_id_token(
                token, app=self._app, check_revoked=self._check_revoked, clock_skew_seconds=10
            )
        except Exception as exc:  # firebase_admin raises several error types
            raise AuthError("invalid Firebase ID token") from exc


class GoogleOidcVerifier:
    def verify(self, token: str, audience: str) -> dict[str, Any]:
        from google.auth.transport import requests as ga_requests
        from google.oauth2 import id_token

        try:
            return id_token.verify_oauth2_token(token, ga_requests.Request(), audience=audience)
        except Exception as exc:
            raise AuthError("invalid OIDC token") from exc


class FirestoreApiClientStore:
    """`api_clients/{client_id}` documents, cached briefly to save free-tier reads."""

    def __init__(self, db: Any, ttl_s: int = 60) -> None:
        self._db = db
        self._cache: TTLCache[str, dict[str, Any] | None] = TTLCache(maxsize=1024, ttl=ttl_s)

    async def get(self, client_id: str) -> dict[str, Any] | None:
        if client_id in self._cache:
            return self._cache[client_id]
        snap = await self._db.collection("api_clients").document(client_id).get()
        record = snap.to_dict() if snap.exists else None
        self._cache[client_id] = record
        return record


class Authenticator:
    def __init__(
        self,
        settings: Settings,
        *,
        firebase: FirebaseTokenVerifier | None,
        clients: ApiClientStore | None,
        oidc: OidcVerifier | None,
    ) -> None:
        self._settings = settings
        self._firebase = firebase
        self._clients = clients
        self._oidc = oidc

    # ---- dev only -------------------------------------------------------------------
    def _dev_principal(self, request: Request) -> Principal | None:
        raw = request.headers.get(DEV_PRINCIPAL_HEADER)
        if raw is None:
            return None
        # Hard stop: never honour the dev header on Cloud Run, whatever ACS_ENV says.
        if not self._settings.is_local or os.environ.get("K_SERVICE"):
            raise AuthError("dev principal header rejected")
        try:
            return Principal.model_validate(json.loads(raw))
        except Exception as exc:
            raise AuthError("malformed dev principal") from exc

    # ---- merchant console -----------------------------------------------------------
    async def _merchant(self, token: str) -> Principal:
        if self._firebase is None:
            raise AuthError("Firebase auth not configured")
        claims = await asyncio.to_thread(self._firebase.verify, token)
        merchant_id = claims.get("merchant_id")
        role = claims.get("role")
        if not isinstance(merchant_id, str) or not merchant_id:
            raise AuthError("token has no merchant_id claim")
        try:
            parsed_role = Role(role)
        except ValueError as exc:
            raise AuthError("token has no valid role claim") from exc
        if claims.get("firebase", {}).get("sign_in_provider") == "anonymous":
            raise AuthError("anonymous sign-in not allowed")
        return Principal(
            kind=PrincipalKind.MERCHANT_USER,
            subject=claims["uid"],
            merchant_id=merchant_id,
            role=parsed_role,
            display=claims.get("email", ""),
        )

    # ---- machine clients (shopper agents, B2B partners) -----------------------------
    async def _client(self, token: str) -> Principal:
        if self._clients is None:
            raise AuthError("API clients not configured")
        body = token.removeprefix(CLIENT_TOKEN_PREFIX)
        client_id, sep, secret = body.partition(".")
        if not sep or not client_id or not secret or len(client_id) > 128:
            raise AuthError("malformed client credential")
        record = await self._clients.get(client_id)
        pepper = self._settings.api_key_pepper.get_secret_value()
        expected = (record or {}).get("secret_hash", "")
        presented = hash_client_secret(secret, pepper)
        # Compare even when the record is missing so timing does not reveal valid ids.
        if not hmac.compare_digest(presented, expected or "0" * 64) or not record:
            raise AuthError("invalid client credential")
        if record.get("disabled"):
            raise AuthError("client disabled")
        try:
            kind = PrincipalKind(record["kind"])
        except (KeyError, ValueError) as exc:
            raise AuthError("client has invalid kind") from exc
        if kind not in (PrincipalKind.SHOPPER_AGENT, PrincipalKind.B2B_PARTNER):
            raise AuthError("client kind not allowed")
        return Principal(
            kind=kind,
            subject=client_id,
            merchant_id=record["merchant_id"],
            ucp_profile=record.get("ucp_profile"),
            display=record.get("display", client_id),
        )

    async def authenticate(self, request: Request, allowed: frozenset[PrincipalKind]) -> Principal:
        principal = self._dev_principal(request)
        if principal is None:
            token = extract_bearer(request)
            if token.startswith(CLIENT_TOKEN_PREFIX):
                principal = await self._client(token)
            else:
                principal = await self._merchant(token)
        if principal.kind not in allowed:
            raise AuthError(f"{principal.kind.value} not accepted on this endpoint")
        return principal

    async def authenticate_gmail_push(self, request: Request) -> Principal:
        principal = self._dev_principal(request)
        if principal is not None:
            return principal
        if (
            self._oidc is None
            or not self._settings.gmail_push_audience
            or not self._settings.gmail_merchant_id
        ):
            raise AuthError("Gmail push not configured")
        token = extract_bearer(request)
        claims = await asyncio.to_thread(
            self._oidc.verify, token, self._settings.gmail_push_audience
        )
        expected_sa = self._settings.gmail_push_service_account
        if not claims.get("email_verified") or claims.get("email") != expected_sa:
            raise AuthError("push token not issued for the expected service account")
        return Principal(
            kind=PrincipalKind.GMAIL_PUSH,
            subject=expected_sa,
            # The monitored B2B inbox belongs to exactly one merchant (tenant).
            merchant_id=self._settings.gmail_merchant_id,
            display="gmail-push",
        )

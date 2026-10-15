"""UCP (Universal Commerce Protocol) surface for AI shopper agents.

* `GET /.well-known/ucp` publishes the business profile (services + capabilities).
* `POST /ucp/v1/refunds` is the REST binding of the refund-decision capability.

UCP requires platforms to send a `UCP-Agent` header (RFC 8941 dictionary with a
`profile` URI) and requires the authenticated principal to be authorized for that
profile, rejecting conflicting identities. We enforce that binding against the
profile registered for the caller's ACS client credential.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from ..logging_setup import audit
from ..models import RefundDecision, RefundRequest
from ..security.principals import AuthError, Principal
from .deps import ShopperAgent, get_container

router = APIRouter(tags=["ucp"])

UCP_VERSION = "2026-04-08"
REFUND_CAPABILITY = "com.acs.shopping.refund_decision"

# key="quoted string" members of an RFC 8941 dictionary
_SF_STRING_MEMBER = re.compile(r'(?:^|,)\s*([a-z*][a-z0-9_.*-]*)\s*=\s*"((?:[^"\\]|\\.)*)"')


def parse_ucp_agent(header: str) -> dict[str, str]:
    if len(header) > 2048:
        raise AuthError("UCP-Agent header too long")
    return {k: re.sub(r"\\(.)", r"\1", v) for k, v in _SF_STRING_MEMBER.findall(header)}


def ucp_profile(base_url: str) -> dict[str, Any]:
    return {
        "ucp": {
            "version": UCP_VERSION,
            "services": {
                "dev.ucp.shopping": [
                    {
                        "version": UCP_VERSION,
                        "spec": "https://ucp.dev/specification/overview/",
                        "transport": "rest",
                        "endpoint": f"{base_url}/ucp/v1",
                        "schema": f"{base_url}/ucp/v1/schemas/refund_request.json",
                    },
                    {
                        "version": UCP_VERSION,
                        "spec": "https://ucp.dev/specification/overview/",
                        "transport": "a2a",
                        "endpoint": f"{base_url}/a2a/.well-known/agent-card.json",
                    },
                ]
            },
            "capabilities": {
                REFUND_CAPABILITY: {
                    "version": UCP_VERSION,
                    "spec": f"{base_url}/ucp/v1/schemas/refund_request.json",
                    "schema": f"{base_url}/ucp/v1/schemas/refund_request.json",
                }
            },
            "payment_handlers": {},
        },
        "keys": [],
    }


@router.get("/.well-known/ucp", include_in_schema=False)
async def well_known_ucp(request: Request) -> JSONResponse:
    base = get_container(request).settings.public_base_url
    return JSONResponse(ucp_profile(base), headers={"Cache-Control": "public, max-age=300"})


@router.get("/ucp/v1/schemas/refund_request.json", include_in_schema=False)
async def refund_schema() -> JSONResponse:
    return JSONResponse(
        RefundRequest.model_json_schema(), headers={"Cache-Control": "public, max-age=300"}
    )


@router.post("/ucp/v1/refunds")
async def ucp_refund(
    body: RefundRequest,
    request: Request,
    principal: Annotated[Principal, Depends(ShopperAgent)],
    ucp_agent: Annotated[str, Header(alias="UCP-Agent")],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    ] = None,
) -> RefundDecision:
    profile = parse_ucp_agent(ucp_agent).get("profile", "")
    if not profile.startswith("https://"):
        raise AuthError("UCP-Agent must carry an https profile URI")
    if not principal.ucp_profile or profile != principal.ucp_profile:
        audit("ucp.identity_mismatch", principal=principal.adk_user_id, outcome="rejected")
        raise AuthError("UCP-Agent profile does not match the authenticated client")
    return await get_container(request).ops.decide_refund(principal, body, idempotency_key)

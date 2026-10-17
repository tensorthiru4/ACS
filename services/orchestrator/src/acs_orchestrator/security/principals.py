"""Authenticated principals and deterministic role-based access control.

Every request is resolved to exactly one Principal by the auth layer. Authorization
decisions are made here, in code, never by the LLM: tool calls are checked against
the same capability table in a before_tool_callback.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class PrincipalKind(StrEnum):
    MERCHANT_USER = "merchant_user"  # Merchant Console user (Firebase Auth)
    SHOPPER_AGENT = "shopper_agent"  # AI shopper agent (UCP / A2A client)
    B2B_PARTNER = "b2b_partner"  # B2B buyer system posting POs (webhook / upload)
    GMAIL_PUSH = "gmail_push"  # Pub/Sub push carrying Gmail notifications


class Role(StrEnum):
    VIEWER = "viewer"
    ANALYST = "analyst"
    ADMIN = "admin"


_ROLE_RANK = {Role.VIEWER: 0, Role.ANALYST: 1, Role.ADMIN: 2}


class Capability(StrEnum):
    CONSOLE_CHAT = "console.chat"
    AUDIT_RUN = "audit.run"
    AUDIT_READ = "audit.read"
    POLICY_COMPILE = "policy.compile"
    POLICY_DEPLOY = "policy.deploy"
    REFUND_DECIDE = "refund.decide"
    INTAKE_SUBMIT = "intake.submit"
    INTAKE_READ = "intake.read"
    ORDER_READ = "order.read"
    ORDER_RELEASE = "order.release"
    A2A_SHOPPER = "a2a.shopper"


# Minimum merchant role per capability. Absent => merchant users may not use it.
_MERCHANT_MIN_ROLE: dict[Capability, Role] = {
    Capability.CONSOLE_CHAT: Role.VIEWER,
    Capability.AUDIT_READ: Role.VIEWER,
    Capability.INTAKE_READ: Role.VIEWER,
    Capability.ORDER_READ: Role.VIEWER,
    Capability.AUDIT_RUN: Role.ANALYST,
    Capability.REFUND_DECIDE: Role.ANALYST,
    Capability.INTAKE_SUBMIT: Role.ANALYST,
    Capability.POLICY_COMPILE: Role.ADMIN,
    Capability.POLICY_DEPLOY: Role.ADMIN,
    # Releasing a draft posts it to the ERP: admins only, and never exposed to the LLM.
    Capability.ORDER_RELEASE: Role.ADMIN,
}

# Capabilities granted to machine principals.
_MACHINE_CAPS: dict[PrincipalKind, frozenset[Capability]] = {
    PrincipalKind.SHOPPER_AGENT: frozenset({Capability.REFUND_DECIDE, Capability.A2A_SHOPPER}),
    PrincipalKind.B2B_PARTNER: frozenset({Capability.INTAKE_SUBMIT}),
    PrincipalKind.GMAIL_PUSH: frozenset({Capability.INTAKE_SUBMIT}),
}


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: PrincipalKind
    subject: str = Field(
        pattern=r"^[A-Za-z0-9_.@-]{1,128}$",
        description="Stable id: Firebase uid, API client id or SA e-mail",
    )
    merchant_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]{1,64}$", description="Tenant the principal acts for"
    )
    role: Role | None = None
    ucp_profile: str | None = None
    display: str = ""

    @property
    def adk_user_id(self) -> str:
        """ADK user id; scopes sessions to tenant + subject so tenants never share sessions."""
        return f"{self.merchant_id}:{self.kind.value}:{self.subject}"

    def can(self, capability: Capability) -> bool:
        if self.kind == PrincipalKind.MERCHANT_USER:
            minimum = _MERCHANT_MIN_ROLE.get(capability)
            return (
                minimum is not None
                and self.role is not None
                and _ROLE_RANK[self.role] >= _ROLE_RANK[minimum]
            )
        return capability in _MACHINE_CAPS.get(self.kind, frozenset())


class AuthError(Exception):
    """Authentication failed (HTTP 401)."""


class ForbiddenError(Exception):
    """Authenticated but not allowed (HTTP 403)."""


def require(principal: Principal, capability: Capability) -> None:
    if not principal.can(capability):
        raise ForbiddenError(f"{principal.kind.value} lacks capability {capability.value}")

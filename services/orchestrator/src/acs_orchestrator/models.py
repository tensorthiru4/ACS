"""Typed request/response contracts. Strict models reject unknown fields everywhere."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ----------------------------------------------------------------------------- refunds
class RefundReason(StrEnum):
    DAMAGED = "damaged"
    NOT_AS_DESCRIBED = "not_as_described"
    NOT_RECEIVED = "not_received"
    LATE_DELIVERY = "late_delivery"
    WRONG_ITEM = "wrong_item"
    CHANGED_MIND = "changed_mind"
    OTHER = "other"


class Money(Strict):
    amount_minor: int = Field(ge=0, le=10_000_000_00, description="Amount in minor units (cents)")
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class RefundRequest(Strict):
    order_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:-]+$")
    amount: Money
    reason: RefundReason
    days_since_delivery: int | None = Field(default=None, ge=0, le=3650)
    item_condition: Literal["unopened", "opened", "used", "damaged", "missing"] | None = None
    customer_note: str | None = Field(default=None, max_length=1000)


class Decision(StrEnum):
    APPROVE = "APPROVE"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


class RuleEngineResult(BaseModel):
    """Response contract of the Deterministic Rule Engine (CEL) `/v1/evaluate`."""

    model_config = ConfigDict(extra="ignore")

    decision: Decision
    reasons: list[str] = Field(default_factory=list, max_length=50)
    matched_rule_ids: list[str] = Field(default_factory=list, max_length=200)
    rule_set_version: str = ""
    max_refund_minor: int | None = Field(default=None, ge=0)


class RefundDecision(BaseModel):
    decision_id: str
    order_id: str
    decision: Decision
    reasons: list[str]
    matched_rule_ids: list[str]
    rule_set_version: str
    approved_amount: Money | None = None
    decided_at: datetime
    source: Literal["rule_engine", "failsafe"]


# ----------------------------------------------------------------------------- intake
class IntakeSource(StrEnum):
    UPLOAD = "upload"
    WEBHOOK = "webhook"
    GMAIL = "gmail"


class WebhookPO(Strict):
    """JSON webhook body used by B2B systems that cannot do multipart uploads."""

    filename: str = Field(max_length=200)
    content_base64: str = Field(max_length=12 * 1024 * 1024)
    customer_ref: str | None = Field(default=None, max_length=128)
    note: str | None = Field(default=None, max_length=2000)


class IntakeReceipt(BaseModel):
    intake_id: str
    status: Literal["queued", "duplicate"]
    content_type: str
    sha256: str
    size: int
    message_id: str | None = None


# ----------------------------------------------------------------------------- audits / policy
class AuditRequest(Strict):
    store_url: str = Field(max_length=2048)
    journeys: list[Literal["discovery", "product_detail", "cart", "checkout", "returns"]] = Field(
        default_factory=lambda: ["discovery", "product_detail", "cart", "checkout"], max_length=5
    )


class PolicyCompileRequest(Strict):
    policy_name: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    policy_text: str = Field(min_length=20, max_length=20_000)
    target_rule_set: Literal["refunds", "order_terms", "pricing"] = "refunds"


class PolicyDeployRequest(Strict):
    expected_version: str | None = Field(default=None, max_length=64)


class A2ATaskView(BaseModel):
    task_id: str | None
    agent: str
    state: str
    result: Any = None


# ----------------------------------------------------------------------------- draft orders
class LineOverride(Strict):
    line_no: int = Field(ge=1, le=200)
    sku: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._:/ -]+$")


class DraftReleaseRequest(Strict):
    """Admin approval of a draft sales order, optionally fixing SKU matches or the buyer."""

    line_overrides: list[LineOverride] = Field(default_factory=list, max_length=200)
    customer_ref: str | None = Field(default=None, max_length=128, pattern=r"^[A-Za-z0-9._@-]+$")


class DraftRejectRequest(Strict):
    reason: str = Field(default="", max_length=500)


# ----------------------------------------------------------------------------- console chat
class ChatMessage(Strict):
    text: str = Field(min_length=1, max_length=8000)

    @field_validator("text")
    @classmethod
    def _no_nul(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("NUL bytes not allowed")
        return v


class ChatReply(BaseModel):
    session_id: str
    reply: str
    tool_calls: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    session_id: str
    last_update_time: float
    title: str | None = None

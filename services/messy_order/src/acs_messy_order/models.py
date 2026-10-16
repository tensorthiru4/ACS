"""PO intake events, model extraction output and draft sales orders."""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "audio/wav",
        "audio/mpeg",
        "audio/ogg",
        "audio/flac",
        "audio/webm",
        "audio/mp4",
        "text/plain",
    }
)

# ISO-4217 minor-unit exponents that differ from the default of 2.
_EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "BHD": 3, "KWD": 3, "OMR": 3,
              "JOD": 3, "TND": 3}  # fmt: skip


def currency_exponent(currency: str) -> int:
    return _EXPONENTS.get(currency, 2)


def to_minor(amount: float | None, currency: str) -> int | None:
    if amount is None:
        return None
    try:
        value = Decimal(str(amount)) * (10 ** currency_exponent(currency))
        return int(value.quantize(Decimal(1), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError):
        return None


class PermanentError(ValueError):
    """The message can never succeed (bad event, not a PO, ...). Ack and mark failed."""


class TransientError(RuntimeError):
    """Retry later (model busy, Firestore contention, ...)."""


# --------------------------------------------------------------------------- Pub/Sub event
class PoAttributes(BaseModel):
    model_config = ConfigDict(extra="ignore")

    schema_version: str = Field(pattern=r"^1$")
    intake_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    merchant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    source: str = Field(pattern=r"^(upload|webhook|gmail)$")
    content_type: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=1)
    filename: str = Field(default="", max_length=200)
    received_at: str = Field(default="", max_length=64)
    submitted_by: str = Field(default="", max_length=256)
    customer_ref: str = Field(default="", max_length=128)

    @field_validator("content_type")
    @classmethod
    def _allowed(cls, value: str) -> str:
        if value not in ALLOWED_CONTENT_TYPES:
            raise ValueError("unsupported content type")
        return value


@dataclass(frozen=True)
class PoEvent:
    attributes: PoAttributes
    data: bytes
    message_id: str
    delivery_attempt: int


def parse_push(envelope: Any, *, max_bytes: int) -> PoEvent:
    """Validate a Pub/Sub push envelope against the PO intake contract."""
    try:
        message = envelope["message"]
        attributes = PoAttributes.model_validate(message.get("attributes") or {})
        raw = message.get("data") or ""
        if len(raw) > (max_bytes * 4) // 3 + 8:
            raise PermanentError("message data exceeds the size limit")
        data = base64.b64decode(raw, validate=True)
    except PermanentError:
        raise
    except (KeyError, TypeError, AttributeError, binascii.Error, ValueError) as exc:
        if isinstance(exc, ValidationError):
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            raise PermanentError(f"invalid attributes: {', '.join(fields)}") from exc
        raise PermanentError("malformed push envelope") from exc
    if len(data) != attributes.size or len(data) > max_bytes:
        raise PermanentError("size attribute does not match the data")
    if hashlib.sha256(data).hexdigest() != attributes.sha256:
        raise PermanentError("sha256 attribute does not match the data")
    if attributes.content_type == "application/pdf" and not data.startswith(b"%PDF-"):
        raise PermanentError("data is not a PDF")
    attempt = envelope.get("deliveryAttempt") if isinstance(envelope, dict) else None
    return PoEvent(
        attributes=attributes,
        data=data,
        message_id=str(message.get("messageId") or message.get("message_id") or "")[:128],
        delivery_attempt=attempt if isinstance(attempt, int) and attempt > 0 else 1,
    )


# --------------------------------------------------------------------------- extraction
class ExtractedLine(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    description: str = Field(min_length=1, max_length=500)
    customer_sku: str | None = Field(default=None, max_length=64)
    quantity: float = Field(gt=0, le=1_000_000)
    uom: str | None = Field(default=None, max_length=32)
    unit_price: float | None = Field(default=None, ge=0, le=1_000_000_000)


class Extraction(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    is_purchase_order: bool
    po_number: str | None = Field(default=None, max_length=64)
    buyer_name: str | None = Field(default=None, max_length=200)
    buyer_reference: str | None = Field(default=None, max_length=128)
    order_date: str | None = Field(default=None, max_length=32)
    requested_delivery_date: str | None = Field(default=None, max_length=32)
    currency: str | None = Field(default=None, max_length=8)
    payment_terms_days: int | None = Field(default=None, ge=0, le=365)
    ship_to: str | None = Field(default=None, max_length=500)
    lines: list[ExtractedLine] = Field(default_factory=list, max_length=200)
    notes: str | None = Field(default=None, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    warnings: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("currency")
    @classmethod
    def _iso(cls, value: str | None) -> str | None:
        value = (value or "").upper()
        return value if len(value) == 3 and value.isalpha() else None

    @field_validator("warnings")
    @classmethod
    def _short(cls, value: list[str]) -> list[str]:
        return [w[:200] for w in value]


EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_purchase_order": {"type": "boolean"},
        "po_number": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "buyer_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "buyer_reference": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "order_date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "YYYY-MM-DD",
        },
        "requested_delivery_date": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "YYYY-MM-DD",
        },
        "currency": {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "description": "ISO-4217 code",
        },
        "payment_terms_days": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "ship_to": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "lines": {
            "type": "array",
            "maxItems": 200,
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "customer_sku": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "quantity": {"type": "number"},
                    "uom": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    "unit_price": {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "description": "Per unit, in major units (e.g. 12.50)",
                    },
                },
                "required": ["description", "quantity"],
            },
        },
        "notes": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "confidence": {"type": "number", "description": "0..1, how sure you are overall"},
        "warnings": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
    },
    "required": ["is_purchase_order", "lines", "confidence"],
}

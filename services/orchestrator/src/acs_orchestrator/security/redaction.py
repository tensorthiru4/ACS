"""Redaction of secrets and PII before anything is logged or echoed back."""

from __future__ import annotations

import re
from typing import Any

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # JWTs (Firebase ID tokens, OIDC tokens)
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    # ACS client credentials and Bearer values
    (re.compile(r"acsk_[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"), "[REDACTED_API_KEY]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{16,}=*"), "Bearer [REDACTED]"),
    # Google API keys
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "[REDACTED_GOOGLE_KEY]"),
    # OAuth refresh tokens
    (re.compile(r"1//[0-9A-Za-z_-]{20,}"), "[REDACTED_REFRESH_TOKEN]"),
    # Payment card numbers (13-19 digits, optional separators)
    (re.compile(r"\b(?:\d[ -]?){12,18}\d\b"), "[REDACTED_PAN]"),
    # E-mail addresses
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
]

_SENSITIVE_KEYS = {
    "authorization",
    "x-forwarded-authorization",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "refresh_token",
    "client_secret",
    "cookie",
}


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact strings, dict values and sensitive keys."""
    if _depth > 8:
        return "[TRUNCATED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if str(k).lower() in _SENSITIVE_KEYS else redact(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value]
    return value

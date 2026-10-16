"""Structured JSON logging for Cloud Logging, with trace correlation and redaction.

Same format as the orchestrator: `audit=true` entries are copied into the BigQuery
audit dataset by the log sink in infra/terraform/data.tf.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from .redaction import redact

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")

_audit_logger = logging.getLogger("acs.audit")


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self._service = service

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": redact(record.getMessage()),
            "logger": record.name,
            "service": self._service,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "request_id": request_id_var.get(),
        }
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update({k: redact(v) for k, v in fields.items()})
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party libraries can log request bodies at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "urllib3", "a2a", "google_genai", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def audit(action: str, *, principal: str, outcome: str, **details: Any) -> None:
    """Emit a security audit event (routed to BigQuery by a log sink filter on audit=true)."""
    _audit_logger.info(
        "audit %s %s",
        action,
        outcome,
        extra={
            "fields": {
                "audit": True,
                "action": action,
                "principal": principal,
                "outcome": outcome,
                "details": details,
            }
        },
    )

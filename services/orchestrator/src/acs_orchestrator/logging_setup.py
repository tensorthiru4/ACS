"""Structured JSON logging for Cloud Logging, with trace correlation and redaction.

Cloud Run ships stdout lines to Cloud Logging; JSON lines with `severity`,
`message` and `logging.googleapis.com/trace` are parsed into structured entries.
A log sink (infra/terraform/bigquery.tf) copies `audit=true` entries into the
BigQuery sandbox audit table.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from .security.redaction import redact

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
trace_var: contextvars.ContextVar[str] = contextvars.ContextVar("trace", default="")

_audit_logger = logging.getLogger("acs.audit")


class JsonFormatter(logging.Formatter):
    def __init__(self, project_id: str) -> None:
        super().__init__()
        self._project_id = project_id

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "severity": record.levelname,
            "message": redact(record.getMessage()),
            "logger": record.name,
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "request_id": request_id_var.get(),
        }
        trace = trace_var.get()
        if trace and self._project_id:
            entry["logging.googleapis.com/trace"] = f"projects/{self._project_id}/traces/{trace}"
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update({k: redact(v) for k, v in fields.items()})
        if record.exc_info:
            entry["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(entry, default=str)


def configure_logging(project_id: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(project_id))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Third-party libraries can log request bodies at DEBUG; keep them quiet.
    for noisy in ("httpx", "httpcore", "google_adk", "urllib3", "a2a"):
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

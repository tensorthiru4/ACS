"""Outbound adapters: raw PO storage, the rule engine and the ERP bridge workflow."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from .models import TransientError

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- "Stores Raw PO"
class RawPoStore(Protocol):
    async def put(
        self, merchant_id: str, intake_id: str, data: bytes, content_type: str
    ) -> str: ...


class GcsRawPoStore:
    """gs://<bucket>/{merchant_id}/{intake_id}; create-only (the SA cannot overwrite or delete)."""

    def __init__(self, bucket: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client().bucket(bucket)
        self._name = bucket

    def _put(self, path: str, data: bytes, content_type: str) -> None:
        from google.api_core.exceptions import PreconditionFailed

        blob = self._bucket.blob(path)
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except PreconditionFailed:
            pass  # stored by an earlier delivery; the object is immutable

    async def put(self, merchant_id: str, intake_id: str, data: bytes, content_type: str) -> str:
        path = f"{merchant_id}/{intake_id}"
        try:
            await asyncio.to_thread(self._put, path, data, content_type)
        except Exception as exc:
            raise TransientError("raw PO upload failed") from exc
        return f"gs://{self._name}/{path}"


class MemoryRawPoStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, merchant_id: str, intake_id: str, data: bytes, content_type: str) -> str:
        path = f"{merchant_id}/{intake_id}"
        self.objects.setdefault(path, data)
        return f"mem://{path}"


# --------------------------------------------------------------------------- "Validates Terms"
@dataclass
class TermsVerdict:
    decision: str
    reasons: list[str]
    matched_rule_ids: list[str] = field(default_factory=list)
    rule_set_version: str = ""
    source: str = "rule_engine"


FAILSAFE = TermsVerdict("ESCALATE", ["rule engine unavailable; routed to human review"],
                        source="failsafe")  # fmt: skip


class TermsChecker:
    """Calls the rule engine with rule_set=order_terms. Any failure fails safe to ESCALATE."""

    def __init__(self, http: httpx.AsyncClient | None) -> None:
        self._http = http

    async def check(self, merchant_id: str, facts: dict[str, Any]) -> TermsVerdict:
        if self._http is None:
            return FAILSAFE
        body = {
            "merchant_id": merchant_id,
            "rule_set": "order_terms",
            "request_id": uuid.uuid4().hex,
            "facts": facts,
        }
        try:
            response = await self._http.post("/v1/evaluate", json=body)
            payload = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            logger.warning("rule engine call failed; escalating")
            return FAILSAFE
        if not isinstance(payload, dict) or payload.get("decision") not in (
            "APPROVE",
            "DENY",
            "ESCALATE",
        ):
            logger.warning("rule engine returned HTTP %s / bad payload", response.status_code)
            return FAILSAFE
        return TermsVerdict(
            decision=payload["decision"],
            reasons=[str(r)[:200] for r in payload.get("reasons", [])][:20],
            matched_rule_ids=[str(r) for r in payload.get("matched_rule_ids", [])][:50],
            rule_set_version=str(payload.get("rule_set_version", "")),
        )


# --------------------------------------------------------------------------- "Draft Sales Order"
class ErpLauncher(Protocol):
    async def start(self, merchant_id: str, draft_id: str) -> str: ...


class WorkflowsErpLauncher:
    """Starts one execution of the ERP Bridge + Safety Gate workflow per approved draft."""

    def __init__(self, project_id: str, region: str, workflow: str) -> None:
        from google.cloud.workflows import executions_v1

        self._types = executions_v1
        # Created on first use: a gRPC aio channel binds to the event loop that is running
        # when it is built, and the app is constructed before uvicorn starts its loop.
        self._client: Any = None
        self._parent = f"projects/{project_id}/locations/{region}/workflows/{workflow}"

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._types.ExecutionsAsyncClient()
        return self._client

    async def start(self, merchant_id: str, draft_id: str) -> str:
        execution = self._types.Execution(
            argument=json.dumps({"merchant_id": merchant_id, "draft_id": draft_id})
        )
        try:
            result = await self._get_client().create_execution(
                parent=self._parent, execution=execution
            )
        except Exception as exc:
            raise TransientError("could not start the ERP bridge workflow") from exc
        return result.name


class MemoryErpLauncher:
    def __init__(self) -> None:
        self.started: list[tuple[str, str]] = []

    async def start(self, merchant_id: str, draft_id: str) -> str:
        self.started.append((merchant_id, draft_id))
        return f"executions/local-{len(self.started)}"

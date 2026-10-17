"""Runs one audit and records the outcome on the persisted A2A task.

Triggered by Cloud Tasks (`POST /internal/audits/{task_id}/run`). The orchestrator
polls `GetTask` from the shared task store, so it sees SUBMITTED -> WORKING ->
COMPLETED/FAILED even across scale-to-zero. A redelivered Cloud Task for a task
that already reached a terminal state is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from a2a.server.context import ServerCallContext
from a2a.server.tasks.task_store import TaskStore
from a2a.types import Artifact, Message, Role, TaskState
from acs_common.a2a_app import data_part, text_part
from acs_common.logging_setup import audit
from google.protobuf import json_format

from .advisor import FixAdvisor
from .browser import UnsafeUrlError
from .checks import SiteSnapshot, as_dicts, failed_checks, overall, score
from .infra import ResultSink

logger = logging.getLogger(__name__)

TERMINAL = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
RunOutcome = Literal["completed", "failed", "skipped", "missing"]


class Collector(Protocol):
    async def collect(self, store_url: str, journeys: list[str]) -> SiteSnapshot: ...


class AuditRunner:
    def __init__(
        self,
        *,
        store: TaskStore,
        collector: Collector,
        advisor: FixAdvisor,
        sink: ResultSink,
        wait_for_task_s: float = 5.0,
    ) -> None:
        self._store = store
        self._collector = collector
        self._advisor = advisor
        self._sink = sink
        self._wait = wait_for_task_s

    async def _save(self, task: Any, state: int, text: str | None = None) -> None:
        task.status.state = state
        task.status.timestamp.FromDatetime(datetime.now(UTC))
        task.status.ClearField("message")
        if text:
            task.status.message.CopyFrom(
                Message(
                    message_id=uuid.uuid4().hex,
                    role=Role.ROLE_AGENT,
                    task_id=task.id,
                    context_id=task.context_id,
                    parts=[text_part(text)],
                )
            )
        await self._store.save(task, ServerCallContext())

    async def _load(self, task_id: str) -> Any:
        deadline = asyncio.get_running_loop().time() + self._wait
        while True:
            task = await self._store.get(task_id, ServerCallContext())
            if task is not None or asyncio.get_running_loop().time() > deadline:
                return task
            await asyncio.sleep(0.25)

    async def run(self, task_id: str) -> RunOutcome:
        task = await self._load(task_id)
        if task is None:
            return "missing"
        if task.status.state in TERMINAL or task.status.state == TaskState.TASK_STATE_WORKING:
            # WORKING: another delivery is (or was) running it; Cloud Tasks will not
            # retry a 2xx, and a crashed run is visible to the orchestrator as stuck.
            return "skipped"
        params = json_format.MessageToDict(task.metadata)
        merchant_id = str(params.get("merchant_id", ""))
        store_url = str(params.get("store_url", ""))
        journeys = [str(j) for j in params.get("journeys", [])]
        await self._save(task, TaskState.TASK_STATE_WORKING)
        try:
            snapshot = await self._collector.collect(store_url, journeys)
            results = score(snapshot, journeys)
            fixes = await self._advisor.suggest(snapshot, failed_checks(results))
            journeys_out = as_dicts(results)
            await self._sink.write(
                audit_id=task_id,
                merchant_id=merchant_id,
                store_url=snapshot.store_url,
                journeys=journeys_out,
                fixes=fixes,
            )
        except UnsafeUrlError as exc:
            await self._save(task, TaskState.TASK_STATE_FAILED, f"store URL rejected: {exc}")
            audit("audit.run", principal="auditor", outcome="unsafe_url", task_id=task_id)
            return "failed"
        except Exception as exc:
            logger.exception("audit %s failed", task_id)
            await self._save(
                task, TaskState.TASK_STATE_FAILED, f"audit failed: {type(exc).__name__}"
            )
            return "failed"

        result = {
            "audit_id": task_id,
            "store_url": snapshot.store_url,
            "score": overall(results),
            "journeys": journeys_out,
            "fixes": fixes,
            "errors": snapshot.errors[:20],
        }
        task.artifacts.append(
            Artifact(artifact_id=uuid.uuid4().hex, name="audit_result", parts=[data_part(result)])
        )
        await self._save(task, TaskState.TASK_STATE_COMPLETED)
        audit(
            "audit.run",
            principal="auditor",
            outcome="completed",
            task_id=task_id,
            merchant_id=merchant_id,
            score=result["score"],
        )
        return "completed"

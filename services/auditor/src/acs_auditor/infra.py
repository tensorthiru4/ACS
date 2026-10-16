"""Audit results to BigQuery ("Writes Scores, Fixes") and Cloud Tasks dispatch."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class ResultSink(Protocol):
    async def write(
        self,
        *,
        audit_id: str,
        merchant_id: str,
        store_url: str,
        journeys: list[dict[str, Any]],
        fixes: list[dict[str, Any]],
    ) -> None: ...


def score_rows(
    audit_id: str, merchant_id: str, store_url: str, journeys: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    return [
        {
            "audit_id": audit_id,
            "merchant_id": merchant_id,
            "store_url": store_url,
            "journey": j["journey"],
            "score": j["score"],
            "details": {"checks": j["checks"]},
            "audited_at": now,
        }
        for j in journeys
    ]


def fix_rows(audit_id: str, merchant_id: str, fixes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    now = datetime.now(UTC).isoformat()
    return [
        {
            "audit_id": audit_id,
            "merchant_id": merchant_id,
            "severity": f["severity"],
            "finding": f["finding"],
            "suggested_fix": f["suggested_fix"],
            "created_at": now,
        }
        for f in fixes
    ]


class BigQuerySink:
    """Batch load jobs (free) rather than streaming inserts (billed per MB)."""

    def __init__(self, project_id: str, dataset: str) -> None:
        from google.cloud import bigquery

        self._bq = bigquery
        self._client = bigquery.Client(project=project_id)
        self._dataset = f"{project_id}.{dataset}"

    def _load(self, table: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        config = self._bq.LoadJobConfig(
            source_format=self._bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            write_disposition=self._bq.WriteDisposition.WRITE_APPEND,
        )
        job = self._client.load_table_from_json(rows, f"{self._dataset}.{table}", job_config=config)
        job.result(timeout=120)

    async def write(self, *, audit_id, merchant_id, store_url, journeys, fixes) -> None:  # type: ignore[no-untyped-def]
        await asyncio.to_thread(
            self._load, "readiness_scores", score_rows(audit_id, merchant_id, store_url, journeys)
        )
        await asyncio.to_thread(self._load, "audit_fixes", fix_rows(audit_id, merchant_id, fixes))


class MemorySink:
    def __init__(self) -> None:
        self.scores: list[dict[str, Any]] = []
        self.fixes: list[dict[str, Any]] = []

    async def write(self, *, audit_id, merchant_id, store_url, journeys, fixes) -> None:  # type: ignore[no-untyped-def]
        self.scores.extend(score_rows(audit_id, merchant_id, store_url, journeys))
        self.fixes.extend(fix_rows(audit_id, merchant_id, fixes))


# --------------------------------------------------------------------------- dispatch
class Dispatcher(Protocol):
    async def dispatch(self, task_id: str) -> None: ...


class CloudTasksDispatcher:
    """Runs each audit as its own authenticated request, so Cloud Run allocates CPU for it.

    The task name is derived from the A2A task id, which makes enqueueing idempotent.
    """

    def __init__(
        self, *, project_id: str, region: str, queue: str, target_url: str, service_account: str
    ) -> None:
        from google.cloud import tasks_v2

        self._tasks = tasks_v2
        self._client = tasks_v2.CloudTasksAsyncClient()
        self._queue = self._client.queue_path(project_id, region, queue)
        self._target = target_url.rstrip("/")
        self._sa = service_account

    async def dispatch(self, task_id: str) -> None:
        from google.api_core.exceptions import AlreadyExists
        from google.protobuf import duration_pb2, timestamp_pb2

        schedule = timestamp_pb2.Timestamp()
        # A short delay lets the A2A handler persist the task before the run starts.
        schedule.FromDatetime(datetime.now(UTC) + timedelta(seconds=2))
        task = self._tasks.Task(
            name=f"{self._queue}/tasks/audit-{task_id}",
            schedule_time=schedule,
            dispatch_deadline=duration_pb2.Duration(seconds=900),
            http_request=self._tasks.HttpRequest(
                http_method=self._tasks.HttpMethod.POST,
                url=f"{self._target}/internal/audits/{task_id}/run",
                oidc_token=self._tasks.OidcToken(
                    service_account_email=self._sa, audience=self._target
                ),
            ),
        )
        try:
            await self._client.create_task(parent=self._queue, task=task)
        except AlreadyExists:
            logger.info("audit %s already scheduled", task_id)


class InProcessDispatcher:
    """Local development only: CPU is always available outside Cloud Run."""

    def __init__(self) -> None:
        self.runner: Any = None
        self._running: set[asyncio.Task[None]] = set()

    async def dispatch(self, task_id: str) -> None:
        async def later() -> None:
            await asyncio.sleep(0.05)
            await self.runner.run(task_id)

        job = asyncio.create_task(later())
        self._running.add(job)
        job.add_done_callback(self._running.discard)

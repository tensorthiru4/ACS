"""Firestore-backed A2A `TaskStore`.

Cloud Run scales the agents to zero, so the SDK's in-memory task store would lose
every task between requests and `GetTask` polls from the orchestrator would 404.
Tasks are kept in `a2a_task_state/{agent}:{task_id}` as protobuf JSON, with an
`expire_at` TTL field (infra/terraform/agents.tf) so old tasks are purged for free.

Tenancy: the only A2A caller is the orchestrator, which records task ownership per
merchant and checks it before every lookup. The store still scopes by the SDK's
owner (the authenticated A2A user) for parity with the in-memory store.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from a2a.server.context import ServerCallContext
from a2a.server.owner_resolver import resolve_user_scope
from a2a.server.tasks.task_store import TaskStore
from a2a.types import a2a_pb2
from a2a.types.a2a_pb2 import Task
from a2a.utils.constants import DEFAULT_LIST_TASKS_PAGE_SIZE
from google.protobuf import json_format

COLLECTION = "a2a_task_state"


class FirestoreTaskStore(TaskStore):
    def __init__(self, db: Any, agent: str, *, ttl_days: int = 30) -> None:
        self._db = db
        self._agent = agent
        self._ttl = timedelta(days=ttl_days)

    def _doc(self, task_id: str) -> Any:
        return self._db.collection(COLLECTION).document(f"{self._agent}:{task_id}")

    async def save(self, task: Task, context: ServerCallContext) -> None:
        now = datetime.now(UTC)
        await self._doc(task.id).set(
            {
                "agent": self._agent,
                "owner": resolve_user_scope(context),
                "task_id": task.id,
                "context_id": task.context_id,
                "state": a2a_pb2.TaskState.Name(task.status.state),
                "task_json": json_format.MessageToJson(task),
                "updated_at": now,
                "expire_at": now + self._ttl,
            }
        )

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        if not task_id or len(task_id) > 128 or "/" in task_id:
            return None
        snap = await self._doc(task_id).get()
        if not snap.exists:
            return None
        record = snap.to_dict() or {}
        if record.get("owner") != resolve_user_scope(context):
            return None
        return json_format.Parse(record["task_json"], Task())

    async def list(
        self, params: a2a_pb2.ListTasksRequest, context: ServerCallContext
    ) -> a2a_pb2.ListTasksResponse:
        from google.cloud.firestore_v1 import FieldFilter

        query = (
            self._db.collection(COLLECTION)
            .where(filter=FieldFilter("agent", "==", self._agent))
            .where(filter=FieldFilter("owner", "==", resolve_user_scope(context)))
        )
        if params.context_id:
            query = query.where(filter=FieldFilter("context_id", "==", params.context_id))
        page_size = params.page_size or DEFAULT_LIST_TASKS_PAGE_SIZE
        tasks: list[Task] = []
        async for snap in query.limit(page_size).stream():
            task = json_format.Parse(snap.to_dict()["task_json"], Task())
            if params.status and task.status.state != params.status:
                continue
            tasks.append(task)
        return a2a_pb2.ListTasksResponse(tasks=tasks, total_size=len(tasks), page_size=page_size)

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        if await self.get(task_id, context) is not None:
            await self._doc(task_id).delete()

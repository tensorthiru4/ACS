from __future__ import annotations

from typing import Any

from a2a.server.context import ServerCallContext
from a2a.types import Artifact, Task, TaskState, TaskStatus

from acs_common.a2a_app import data_part
from acs_common.a2a_store import FirestoreTaskStore


class _Snap:
    def __init__(self, data: dict[str, Any] | None) -> None:
        self._data = data
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return dict(self._data) if self._data is not None else None


class _Doc:
    def __init__(self, store: dict[str, dict[str, Any]], key: str) -> None:
        self._store, self._key = store, key

    async def set(self, data: dict[str, Any]) -> None:
        self._store[self._key] = dict(data)

    async def get(self) -> _Snap:
        return _Snap(self._store.get(self._key))

    async def delete(self) -> None:
        self._store.pop(self._key, None)


class FakeDb:
    def __init__(self) -> None:
        self.docs: dict[str, dict[str, Any]] = {}

    def collection(self, name: str) -> Any:
        assert name == "a2a_task_state"
        docs = self.docs
        return type("C", (), {"document": lambda _self, key: _Doc(docs, key)})()


async def test_round_trip_survives_a_new_store_instance():
    db = FakeDb()
    ctx = ServerCallContext()
    task = Task(
        id="t-1",
        context_id="c-1",
        status=TaskStatus(state=TaskState.TASK_STATE_COMPLETED),
        artifacts=[Artifact(artifact_id="a", parts=[data_part({"score": 72.5})])],
    )
    await FirestoreTaskStore(db, "auditor").save(task, ctx)
    record = db.docs["auditor:t-1"]
    assert record["state"] == "TASK_STATE_COMPLETED" and "expire_at" in record

    # A different instance (scale-to-zero) reads the same task back.
    loaded = await FirestoreTaskStore(db, "auditor").get("t-1", ctx)
    assert loaded == task
    # Scoped per agent, and ids that could address other docs are refused.
    assert await FirestoreTaskStore(db, "policy_compiler").get("t-1", ctx) is None
    assert await FirestoreTaskStore(db, "auditor").get("../x", ctx) is None

    await FirestoreTaskStore(db, "auditor").delete("t-1", ctx)
    assert db.docs == {}

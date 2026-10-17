"""A2A executor for `audit_store`: validate, persist a SUBMITTED task, schedule the run.

The orchestrator sends `returnImmediately=true`, so this returns as soon as the task
exists; the audit itself runs in `runner.py` under its own Cloud Tasks request.
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Task, TaskState, TaskStatus
from acs_common.a2a_app import data_payload, text_part
from google.protobuf import json_format, struct_pb2
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .browser import UnsafeUrlError, parse_store_url, resolve_public
from .checks import JOURNEYS
from .infra import Dispatcher

logger = logging.getLogger(__name__)


class AuditPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    skill: str = Field(pattern=r"^audit_store$")
    merchant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    store_url: str = Field(max_length=2048)
    journeys: list[str] = Field(default_factory=lambda: list(JOURNEYS[:4]), max_length=5)
    requested_by: str = Field(default="", max_length=128)

    def checked_journeys(self) -> list[str]:
        unknown = set(self.journeys) - set(JOURNEYS)
        if unknown or not self.journeys:
            raise ValueError("unknown journeys")
        return list(dict.fromkeys(self.journeys))


class AuditExecutor(AgentExecutor):
    def __init__(self, dispatcher: Dispatcher, *, resolve_dns: bool = True) -> None:
        self._dispatcher = dispatcher
        self._resolve = resolve_dns

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            payload = AuditPayload.model_validate(data_payload(context.message))
            journeys = payload.checked_journeys()
            url, host = parse_store_url(payload.store_url)
            if self._resolve:
                await resolve_public(host)
        except (ValidationError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, UnsafeUrlError) else "invalid audit request"
            # The first event is what a returnImmediately caller sees, so reject in one event.
            rejected = self._task(context, TaskState.TASK_STATE_REJECTED)
            rejected.status.message.CopyFrom(updater.new_agent_message([text_part(reason)]))
            await event_queue.enqueue_event(rejected)
            return

        metadata = struct_pb2.Struct()
        json_format.ParseDict(
            {"merchant_id": payload.merchant_id, "store_url": url, "journeys": journeys,
             "requested_by": payload.requested_by},
            metadata,
        )  # fmt: skip
        await event_queue.enqueue_event(
            self._task(context, TaskState.TASK_STATE_SUBMITTED, metadata)
        )
        try:
            await self._dispatcher.dispatch(context.task_id)
        except Exception:
            logger.exception("could not schedule audit %s", context.task_id)
            await updater.failed(updater.new_agent_message([text_part("could not schedule")]))

    @staticmethod
    def _task(context: RequestContext, state: int, metadata: struct_pb2.Struct | None = None):  # type: ignore[no-untyped-def]
        task = Task(
            id=context.task_id, context_id=context.context_id, status=TaskStatus(state=state)
        )
        if metadata is not None:
            task.metadata.CopyFrom(metadata)
        return task

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return None

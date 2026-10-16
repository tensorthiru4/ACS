"""A2A executor: routes `compile_policy` / `deploy_rules` DataParts to the compiler.

Outcomes map onto A2A task states the orchestrator surfaces to the console:
  COMPLETED  draft created / rules deployed (DataPart artifact)
  REJECTED   bad input, unknown draft, version conflict, approver not allowed
  FAILED     the model could not produce valid rules, or was unavailable
"""

from __future__ import annotations

import logging

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Task, TaskState, TaskStatus
from acs_common.a2a_app import data_part, data_payload, text_part
from acs_common.gemini import ModelError
from acs_common.logging_setup import audit
from pydantic import ValidationError

from .compiler import CompileError, CompileRequest, DeployRequest, PolicyCompiler
from .repository import ApproverError, DraftError, VersionConflict

logger = logging.getLogger(__name__)


class CompilerExecutor(AgentExecutor):
    def __init__(self, compiler: PolicyCompiler) -> None:
        self._compiler = compiler

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        async def finish(state: str, text: str) -> None:
            message = updater.new_agent_message([text_part(text)])
            if state == "rejected":
                await updater.reject(message)
            else:
                await updater.failed(message)

        payload = data_payload(context.message)
        skill = payload.get("skill")
        merchant = str(payload.get("merchant_id", ""))[:64]
        try:
            if skill == "compile_policy":
                compile_request = CompileRequest.model_validate(payload)
                result = await self._compiler.compile(compile_request)
                principal = compile_request.requested_by
            elif skill == "deploy_rules":
                deploy_request = DeployRequest.model_validate(payload)
                result = await self._compiler.deploy(deploy_request)
                principal = deploy_request.approved_by
            else:
                await finish("rejected", "unknown skill; expected compile_policy or deploy_rules")
                return
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
            await finish("rejected", f"invalid request: {', '.join(fields)}")
            return
        except VersionConflict as exc:
            audit("policy.deploy", principal="orchestrator", outcome="conflict", merchant=merchant)
            await finish("rejected", str(exc))
            return
        except (DraftError, ApproverError) as exc:
            await finish("rejected", str(exc))
            return
        except CompileError as exc:
            await finish("failed", f"{exc}: " + "; ".join(exc.problems[:10]))
            return
        except ModelError as exc:
            hint = " (busy or out of quota; try again shortly)" if exc.transient else ""
            await finish("failed", f"policy model unavailable{hint}")
            return

        audit(
            "policy.compile" if skill == "compile_policy" else "policy.deploy",
            principal=principal,
            outcome="ok",
            merchant=merchant,
            draft_id=result.get("draft_id"),
            version=result.get("version"),
        )
        await updater.add_artifact([data_part(result)], name=str(skill))
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        return None

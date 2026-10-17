"""Domain operations behind every orchestrator capability.

Both the REST endpoints and the ADK tools call these functions, so authorization,
tenant scoping, validation, idempotency and audit logging are identical whether
an action is requested by a human, by an AI shopper agent, or by the LLM.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .clients.a2a_client import A2AAgentClient, A2ACallError, A2AResult
from .clients.gmail import GmailReader
from .clients.order_desk import OrderDeskClient, OrderDeskError
from .clients.pubsub import PoPublisher
from .clients.rule_engine import RuleEngineClient, RuleEngineError
from .clients.store import Store, intake_id_for
from .config import Settings
from .logging_setup import audit
from .models import (
    A2ATaskView,
    AuditRequest,
    Decision,
    DraftRejectRequest,
    DraftReleaseRequest,
    IntakeReceipt,
    IntakeSource,
    Money,
    PolicyCompileRequest,
    PolicyDeployRequest,
    RefundDecision,
    RefundRequest,
)
from .security.files import validate_po_file
from .security.principals import Capability, ForbiddenError, Principal, PrincipalKind, require
from .security.ssrf import validate_storefront_url

logger = logging.getLogger(__name__)

AUDITOR = "auditor"
POLICY_COMPILER = "policy_compiler"
POLICY_DRAFT = "policy_draft"

_DRAFT_ID = re.compile(r"^[0-9a-f]{40}$")
_DRAFT_STATUSES = frozenset(
    {"approved", "needs_review", "rejected", "posting", "posted", "erp_failed", "erp_blocked"}
)
# Draft fields shown to merchant users (internal storage paths and raw facts stay inside).
_DRAFT_VIEW_KEYS = (
    "intake_id", "status", "source", "po_number", "order_date", "requested_delivery_date",
    "payment_terms_days", "currency", "customer", "lines", "total_minor", "extraction",
    "terms", "approved_by", "approved_at", "rejected_by", "rejection_reason", "erp",
    "created_at", "updated_at",
)  # fmt: skip
_DRAFT_SUMMARY_KEYS = ("intake_id", "status", "po_number", "currency", "total_minor",
                       "created_at")  # fmt: skip


class NotFoundError(LookupError):
    pass


class ConflictError(RuntimeError):
    pass


@dataclass
class Operations:
    settings: Settings
    store: Store
    publisher: PoPublisher
    rule_engine: RuleEngineClient | None
    auditor: A2AAgentClient | None
    policy_compiler: A2AAgentClient | None
    gmail: GmailReader | None = None
    resolve_dns: bool = True
    order_desk: OrderDeskClient | None = None

    # ================================================================== refunds
    async def decide_refund(
        self, principal: Principal, request: RefundRequest, idempotency_key: str | None = None
    ) -> RefundDecision:
        require(principal, Capability.REFUND_DECIDE)
        request_hash = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        key_material = idempotency_key or request_hash
        store_key = (
            f"{principal.merchant_id}:" + hashlib.sha256(key_material.encode()).hexdigest()[:40]
        )

        existing = await self.store.get_refund_decision(store_key)
        if existing:
            if existing.get("request_hash") != request_hash:
                raise ConflictError("idempotency key reused with a different request")
            return RefundDecision.model_validate(existing["decision"])

        facts = {
            "order_id": request.order_id,
            "requested_amount_minor": request.amount.amount_minor,
            "currency": request.amount.currency,
            "reason": request.reason.value,
            "days_since_delivery": request.days_since_delivery,
            "item_condition": request.item_condition,
            "channel": principal.kind.value,
            # Facts asserted by an external AI agent are claims, not verified data;
            # CEL rule sets are expected to escalate unverified high-value requests.
            "facts_verified": principal.kind == PrincipalKind.MERCHANT_USER,
        }
        decision_id = uuid.uuid4().hex
        try:
            result = await self.rule_engine_or_raise().evaluate(
                merchant_id=principal.merchant_id,
                rule_set="refunds",
                facts=facts,
                request_id=decision_id,
            )
            approved = None
            if result.decision == Decision.APPROVE:
                cap = request.amount.amount_minor
                if result.max_refund_minor is not None:
                    cap = min(cap, result.max_refund_minor)
                approved = Money(amount_minor=cap, currency=request.amount.currency)
            decision = RefundDecision(
                decision_id=decision_id,
                order_id=request.order_id,
                decision=result.decision,
                reasons=result.reasons,
                matched_rule_ids=result.matched_rule_ids,
                rule_set_version=result.rule_set_version,
                approved_amount=approved,
                decided_at=datetime.now(UTC),
                source="rule_engine",
            )
        except RuleEngineError as exc:
            # Fail safe: never approve without a rule engine verdict.
            logger.error("rule engine failure, escalating refund: %s", exc)
            decision = RefundDecision(
                decision_id=decision_id,
                order_id=request.order_id,
                decision=Decision.ESCALATE,
                reasons=["rule engine unavailable; routed to human review"],
                matched_rule_ids=[],
                rule_set_version="",
                decided_at=datetime.now(UTC),
                source="failsafe",
            )

        if decision.source == "rule_engine":
            saved = await self.store.save_refund_decision(
                store_key,
                {
                    "request_hash": request_hash,
                    "merchant_id": principal.merchant_id,
                    "decision": decision.model_dump(mode="json"),
                },
            )
            if not saved:  # a concurrent duplicate won the race; return its decision
                winner = await self.store.get_refund_decision(store_key)
                if winner:
                    decision = RefundDecision.model_validate(winner["decision"])
        audit(
            "refund.decide",
            principal=principal.adk_user_id,
            outcome=decision.decision.value,
            order_id=request.order_id,
            decision_id=decision.decision_id,
            rule_set_version=decision.rule_set_version,
        )
        return decision

    def rule_engine_or_raise(self) -> RuleEngineClient:
        if self.rule_engine is None:
            raise RuleEngineError("rule engine not configured")
        return self.rule_engine

    # ================================================================== audits (A2A)
    async def start_audit(self, principal: Principal, request: AuditRequest) -> A2ATaskView:
        require(principal, Capability.AUDIT_RUN)
        domains = await self.store.storefront_domains(principal.merchant_id)
        url = await validate_storefront_url(request.store_url, domains, resolve=self.resolve_dns)
        auditor = self._agent(self.auditor, "Agent-Readiness Auditor")
        result = await auditor.send(
            {
                "skill": "audit_store",
                "merchant_id": principal.merchant_id,
                "store_url": url,
                "journeys": list(request.journeys),
                "requested_by": principal.subject,
            },
            metadata={"merchant_id": principal.merchant_id},
            wait=False,
        )
        await self._own(AUDITOR, result, principal)
        audit(
            "audit.start",
            principal=principal.adk_user_id,
            outcome=result.state,
            store_url=url,
            task_id=result.task_id,
        )
        return self._view(AUDITOR, result)

    async def get_audit(self, principal: Principal, task_id: str) -> A2ATaskView:
        require(principal, Capability.AUDIT_READ)
        await self._check_owner(AUDITOR, task_id, principal)
        result = await self._agent(self.auditor, "Agent-Readiness Auditor").get_task(task_id)
        return self._view(AUDITOR, result)

    # ================================================================== policy (A2A)
    async def compile_policy(
        self, principal: Principal, request: PolicyCompileRequest
    ) -> A2ATaskView:
        """Compile natural-language policy into a *draft* rule set (never deployed here)."""
        require(principal, Capability.POLICY_COMPILE)
        compiler = self._agent(self.policy_compiler, "Policy-as-Code Compiler")
        result = await compiler.send(
            {
                "skill": "compile_policy",
                "merchant_id": principal.merchant_id,
                "policy_name": request.policy_name,
                "target_rule_set": request.target_rule_set,
                "policy_text": request.policy_text,
                "dry_run": True,
                "requested_by": principal.subject,
            },
            metadata={"merchant_id": principal.merchant_id},
            wait=True,
        )
        await self._own(POLICY_COMPILER, result, principal)
        for item in result.data:
            draft_id = item.get("draft_id")
            if isinstance(draft_id, str) and draft_id:
                await self.store.record_task(
                    POLICY_DRAFT,
                    draft_id,
                    {"merchant_id": principal.merchant_id, "subject": principal.subject},
                )
        audit(
            "policy.compile",
            principal=principal.adk_user_id,
            outcome=result.state,
            policy_name=request.policy_name,
            task_id=result.task_id,
        )
        return self._view(POLICY_COMPILER, result)

    async def deploy_policy(
        self, principal: Principal, draft_id: str, request: PolicyDeployRequest
    ) -> A2ATaskView:
        """Human-approved deployment. Deliberately NOT exposed as an LLM tool."""
        require(principal, Capability.POLICY_DEPLOY)
        await self._check_owner(POLICY_DRAFT, draft_id, principal)
        compiler = self._agent(self.policy_compiler, "Policy-as-Code Compiler")
        result = await compiler.send(
            {
                "skill": "deploy_rules",
                "merchant_id": principal.merchant_id,
                "draft_id": draft_id,
                "expected_version": request.expected_version,
                "approved_by": principal.subject,
            },
            metadata={"merchant_id": principal.merchant_id},
            wait=True,
        )
        audit(
            "policy.deploy",
            principal=principal.adk_user_id,
            outcome=result.state,
            draft_id=draft_id,
        )
        return self._view(POLICY_COMPILER, result)

    # ================================================================== PO intake
    async def submit_po(
        self,
        principal: Principal,
        data: bytes,
        *,
        filename: str | None,
        source: IntakeSource,
        customer_ref: str | None = None,
        allow_text: bool = False,
    ) -> IntakeReceipt:
        require(principal, Capability.INTAKE_SUBMIT)
        file = validate_po_file(
            data, filename=filename, max_bytes=self.settings.max_po_bytes, allow_text=allow_text
        )
        intake_id = intake_id_for(principal.merchant_id, file.sha256)
        record = {
            "merchant_id": principal.merchant_id,
            "source": source.value,
            "content_type": file.content_type,
            "sha256": file.sha256,
            "size": file.size,
            "filename": file.filename,
            "submitted_by": principal.adk_user_id,
            "customer_ref": customer_ref or "",
            "status": "claimed",
        }
        if not await self.store.claim_intake(intake_id, record):
            existing = await self.store.get_intake(intake_id) or {}
            if existing.get("status") != "failed":
                return IntakeReceipt(
                    intake_id=intake_id,
                    status="duplicate",
                    content_type=file.content_type,
                    sha256=file.sha256,
                    size=file.size,
                    message_id=existing.get("message_id"),
                )
        attributes = {
            "schema_version": "1",
            "intake_id": intake_id,
            "merchant_id": principal.merchant_id,
            "source": source.value,
            "content_type": file.content_type,
            "sha256": file.sha256,
            "size": str(file.size),
            "filename": file.filename,
            "received_at": datetime.now(UTC).isoformat(),
            "submitted_by": principal.adk_user_id,
        }
        if customer_ref:
            attributes["customer_ref"] = customer_ref[:128]
        try:
            message_id = await self.publisher.publish(data, attributes)
        except Exception:
            await self.store.update_intake(intake_id, {"status": "failed"})
            audit(
                "intake.publish",
                principal=principal.adk_user_id,
                outcome="failed",
                intake_id=intake_id,
            )
            raise
        await self.store.update_intake(intake_id, {"status": "queued", "message_id": message_id})
        audit(
            "intake.publish",
            principal=principal.adk_user_id,
            outcome="queued",
            intake_id=intake_id,
            content_type=file.content_type,
            size=file.size,
            source=source.value,
        )
        return IntakeReceipt(
            intake_id=intake_id,
            status="queued",
            content_type=file.content_type,
            sha256=file.sha256,
            size=file.size,
            message_id=message_id,
        )

    async def submit_po_base64(
        self, principal: Principal, content_base64: str, **kwargs: Any
    ) -> IntakeReceipt:
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            from .security.files import RejectedFileError

            raise RejectedFileError("content_base64 is not valid base64") from exc
        return await self.submit_po(principal, data, **kwargs)

    async def get_intake(self, principal: Principal, intake_id: str) -> dict[str, Any]:
        require(principal, Capability.INTAKE_READ)
        record = await self.store.get_intake(intake_id)
        if not record or record.get("merchant_id") != principal.merchant_id:
            raise NotFoundError("intake not found")
        keys = ("status", "source", "content_type", "size", "filename", "created_at", "message_id")
        return {"intake_id": intake_id, **{k: record.get(k) for k in keys}}

    async def process_gmail_push(self, principal: Principal, envelope: dict[str, Any]) -> int:
        """Handle one Pub/Sub push envelope from Gmail `users.watch`. Returns POs queued."""
        if principal.kind != PrincipalKind.GMAIL_PUSH:
            raise ForbiddenError("Gmail push only")
        try:
            raw = envelope["message"]["data"]
            notification = json.loads(base64.b64decode(raw))
            mailbox = str(notification["emailAddress"]).lower()
            history_id = str(int(notification["historyId"]))
        except (KeyError, ValueError, TypeError, binascii.Error):
            logger.warning("gmail push: malformed envelope ignored")
            return 0
        if mailbox != self.settings.gmail_mailbox.lower():
            logger.warning("gmail push: notification for unexpected mailbox ignored")
            return 0
        if self.gmail is None:
            raise RuntimeError("Gmail reader not configured")

        start = await self.store.get_gmail_history(mailbox)
        if not start:  # first notification, or cursor reset after expiry
            await self.store.set_gmail_history(mailbox, history_id)
            return 0
        candidates, latest = await self.gmail.fetch_since(start)
        queued = 0
        for candidate in candidates:
            try:
                receipt = await self.submit_po(
                    principal,
                    candidate.data,
                    filename=candidate.filename,
                    source=IntakeSource.GMAIL,
                    customer_ref=candidate.sender_domain,
                    allow_text=candidate.is_body,
                )
                queued += receipt.status == "queued"
            except ValueError as exc:  # RejectedFileError and friends: skip, keep going
                logger.info("gmail push: candidate rejected: %s", exc)
        await self.store.set_gmail_history(mailbox, latest)
        return queued

    # ================================================================== draft sales orders
    async def _own_draft(self, principal: Principal, intake_id: str) -> dict[str, Any]:
        if not _DRAFT_ID.match(intake_id or ""):
            raise NotFoundError("draft not found")
        draft = await self.store.get_draft_order(intake_id)
        if not draft or draft.get("merchant_id") != principal.merchant_id:
            raise NotFoundError("draft not found")
        return draft

    async def list_draft_orders(
        self, principal: Principal, status: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        require(principal, Capability.ORDER_READ)
        if status is not None and status not in _DRAFT_STATUSES:
            raise NotFoundError("unknown status")
        drafts = await self.store.list_draft_orders(principal.merchant_id, min(max(limit, 1), 100))
        return [
            {k: d.get(k) for k in _DRAFT_SUMMARY_KEYS}
            for d in drafts
            if status is None or d.get("status") == status
        ]

    async def get_draft_order(self, principal: Principal, intake_id: str) -> dict[str, Any]:
        require(principal, Capability.ORDER_READ)
        draft = await self._own_draft(principal, intake_id)
        return {k: draft.get(k) for k in _DRAFT_VIEW_KEYS}

    def _desk(self) -> OrderDeskClient:
        if self.order_desk is None:
            raise OrderDeskError("Messy-Order-to-ERP agent is not configured")
        return self.order_desk

    async def release_draft_order(
        self, principal: Principal, intake_id: str, request: DraftReleaseRequest
    ) -> dict[str, Any]:
        """Human approval: the agent re-validates and starts the ERP bridge. Not an LLM tool."""
        require(principal, Capability.ORDER_RELEASE)
        await self._own_draft(principal, intake_id)
        result = await self._desk().release(
            intake_id,
            merchant_id=principal.merchant_id,
            approved_by=principal.adk_user_id,
            line_overrides=[o.model_dump() for o in request.line_overrides],
            customer_ref=request.customer_ref,
        )
        audit(
            "order.release",
            principal=principal.adk_user_id,
            outcome=str(result.get("status")),
            intake_id=intake_id,
            overrides=len(request.line_overrides),
        )
        return result

    async def reject_draft_order(
        self, principal: Principal, intake_id: str, request: DraftRejectRequest
    ) -> dict[str, Any]:
        require(principal, Capability.ORDER_RELEASE)
        await self._own_draft(principal, intake_id)
        result = await self._desk().reject(
            intake_id,
            merchant_id=principal.merchant_id,
            rejected_by=principal.adk_user_id,
            reason=request.reason,
        )
        audit("order.reject", principal=principal.adk_user_id, outcome="rejected",
              intake_id=intake_id)  # fmt: skip
        return result

    # ================================================================== helpers
    @staticmethod
    def _agent(client: A2AAgentClient | None, name: str) -> A2AAgentClient:
        if client is None:
            raise A2ACallError(f"{name} is not configured")
        return client

    async def _own(self, agent: str, result: A2AResult, principal: Principal) -> None:
        if result.task_id:
            await self.store.record_task(
                agent,
                result.task_id,
                {"merchant_id": principal.merchant_id, "subject": principal.subject},
            )

    async def _check_owner(self, agent: str, task_id: str, principal: Principal) -> None:
        if not task_id or len(task_id) > 128:
            raise NotFoundError("task not found")
        record = await self.store.get_task(agent, task_id)
        if not record or record.get("merchant_id") != principal.merchant_id:
            raise NotFoundError("task not found")

    @staticmethod
    def _view(agent: str, result: A2AResult) -> A2ATaskView:
        return A2ATaskView(
            task_id=result.task_id,
            agent=agent,
            state=result.state,
            result={"data": result.data, "text": result.text}
            if (result.data or result.text)
            else None,
        )

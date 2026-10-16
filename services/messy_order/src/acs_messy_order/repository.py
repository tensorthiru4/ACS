"""Firestore state for PO intake, draft sales orders and customer mappings.

Collections:
  po_intake/{intake_id}              created by the orchestrator; this agent leases it
                                     (status processing -> drafted | failed)
  draft_sales_orders/{intake_id}     the "Draft Sales Order" read by the ERP bridge
  customers/{merchant_id}:{ref}      buyer reference -> Odoo partner, agreed terms

Idempotency: a delivery must win the intake lease before doing any work; drafts are
created once and never overwritten by a redelivery.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

LeaseResult = Literal["start", "done", "busy", "mismatch"]
DONE_STATUSES = frozenset({"drafted", "failed"})
RELEASABLE = frozenset({"needs_review", "erp_failed"})
REJECTABLE = frozenset({"needs_review", "approved", "erp_failed"})


class DraftStateError(RuntimeError):
    """The draft is not in a state that allows the requested transition (HTTP 409)."""


class DraftNotFound(LookupError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def decide_lease(record: dict[str, Any] | None, merchant_id: str, now: datetime) -> LeaseResult:
    if record is None:
        return "start"
    if record.get("merchant_id") != merchant_id:
        return "mismatch"
    status = record.get("status")
    if status in DONE_STATUSES and record.get("processed_by_agent"):
        return "done"
    lease_until = record.get("lease_until")
    if status == "processing" and isinstance(lease_until, datetime) and lease_until > now:
        return "busy"
    return "start"


class Repository(Protocol):
    async def lease_intake(
        self, intake_id: str, merchant_id: str, message_id: str, lease_s: int
    ) -> LeaseResult: ...
    async def release_intake(self, intake_id: str, error: str) -> None: ...
    async def finish_intake(self, intake_id: str, status: str, fields: dict[str, Any]) -> None: ...
    async def get_draft(self, intake_id: str) -> dict[str, Any] | None: ...
    async def create_draft(self, draft: dict[str, Any]) -> bool: ...
    async def update_draft(self, intake_id: str, fields: dict[str, Any]) -> None: ...
    async def transition_draft(
        self, intake_id: str, merchant_id: str, allowed: frozenset[str], mutate: Any
    ) -> dict[str, Any]: ...
    async def customer(self, merchant_id: str, customer_ref: str) -> dict[str, Any] | None: ...


class FirestoreRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    def _intake(self, intake_id: str) -> Any:
        return self._db.collection("po_intake").document(intake_id)

    def _draft(self, intake_id: str) -> Any:
        return self._db.collection("draft_sales_orders").document(intake_id)

    async def lease_intake(
        self, intake_id: str, merchant_id: str, message_id: str, lease_s: int
    ) -> LeaseResult:
        from google.cloud import firestore

        ref = self._intake(intake_id)

        @firestore.async_transactional
        async def run(transaction: Any) -> LeaseResult:
            snap = await ref.get(transaction=transaction)
            record = snap.to_dict() if snap.exists else None
            now = _now()
            result = decide_lease(record, merchant_id, now)
            if result == "start":
                fields = {
                    "status": "processing",
                    "lease_until": now + timedelta(seconds=lease_s),
                    "processing_message_id": message_id,
                    "attempts": firestore.Increment(1),
                    "updated_at": now,
                }
                if record is None:
                    transaction.set(ref, {**fields, "merchant_id": merchant_id, "attempts": 1})
                else:
                    transaction.update(ref, fields)
            return result

        return await run(self._db.transaction())

    async def release_intake(self, intake_id: str, error: str) -> None:
        await self._intake(intake_id).update(
            {"status": "retrying", "lease_until": None, "last_error": error[:300],
             "updated_at": _now()}
        )  # fmt: skip

    async def finish_intake(self, intake_id: str, status: str, fields: dict[str, Any]) -> None:
        await self._intake(intake_id).update(
            {**fields, "status": status, "lease_until": None, "processed_by_agent": True,
             "updated_at": _now()}
        )  # fmt: skip

    async def get_draft(self, intake_id: str) -> dict[str, Any] | None:
        snap = await self._draft(intake_id).get()
        return snap.to_dict() if snap.exists else None

    async def create_draft(self, draft: dict[str, Any]) -> bool:
        from google.api_core.exceptions import AlreadyExists

        now = _now()
        try:
            await self._draft(draft["intake_id"]).create(
                {**draft, "created_at": now, "updated_at": now}
            )
            return True
        except AlreadyExists:
            return False

    async def update_draft(self, intake_id: str, fields: dict[str, Any]) -> None:
        await self._draft(intake_id).update({**fields, "updated_at": _now()})

    async def transition_draft(
        self, intake_id: str, merchant_id: str, allowed: frozenset[str], mutate: Any
    ) -> dict[str, Any]:
        """Read-check-write a draft in one transaction. `mutate(draft) -> fields` (sync)."""
        from google.cloud import firestore

        ref = self._draft(intake_id)

        @firestore.async_transactional
        async def run(transaction: Any) -> dict[str, Any]:
            snap = await ref.get(transaction=transaction)
            draft = snap.to_dict() if snap.exists else None
            if not draft or draft.get("merchant_id") != merchant_id:
                raise DraftNotFound("draft not found")
            if draft.get("status") not in allowed:
                raise DraftStateError(f"draft is {draft.get('status')}")
            fields = {**mutate(draft), "updated_at": _now()}
            transaction.update(ref, fields)
            return {**draft, **fields}

        return await run(self._db.transaction())

    async def customer(self, merchant_id: str, customer_ref: str) -> dict[str, Any] | None:
        if not customer_ref or "/" in customer_ref:
            return None
        snap = (
            await self._db.collection("customers")
            .document(f"{merchant_id}:{customer_ref.lower()}")
            .get()
        )
        return snap.to_dict() if snap.exists else None


class MemoryRepository:
    """In-process repository for local development and tests (same semantics)."""

    def __init__(self) -> None:
        self.intake: dict[str, dict[str, Any]] = {}
        self.drafts: dict[str, dict[str, Any]] = {}
        self.customers: dict[str, dict[str, Any]] = {}

    async def lease_intake(
        self, intake_id: str, merchant_id: str, message_id: str, lease_s: int
    ) -> LeaseResult:
        record = self.intake.get(intake_id)
        now = _now()
        result = decide_lease(record, merchant_id, now)
        if result == "start":
            record = self.intake.setdefault(intake_id, {"merchant_id": merchant_id})
            record.update(
                status="processing",
                lease_until=now + timedelta(seconds=lease_s),
                processing_message_id=message_id,
                attempts=record.get("attempts", 0) + 1,
            )
        return result

    async def release_intake(self, intake_id: str, error: str) -> None:
        self.intake[intake_id].update(status="retrying", lease_until=None, last_error=error)

    async def finish_intake(self, intake_id: str, status: str, fields: dict[str, Any]) -> None:
        self.intake[intake_id].update(
            {**fields, "status": status, "lease_until": None, "processed_by_agent": True}
        )

    async def get_draft(self, intake_id: str) -> dict[str, Any] | None:
        draft = self.drafts.get(intake_id)
        return copy.deepcopy(draft) if draft else None

    async def create_draft(self, draft: dict[str, Any]) -> bool:
        if draft["intake_id"] in self.drafts:
            return False
        self.drafts[draft["intake_id"]] = copy.deepcopy(draft)
        return True

    async def update_draft(self, intake_id: str, fields: dict[str, Any]) -> None:
        self.drafts[intake_id].update(copy.deepcopy(fields))

    async def transition_draft(
        self, intake_id: str, merchant_id: str, allowed: frozenset[str], mutate: Any
    ) -> dict[str, Any]:
        draft = self.drafts.get(intake_id)
        if not draft or draft.get("merchant_id") != merchant_id:
            raise DraftNotFound("draft not found")
        if draft.get("status") not in allowed:
            raise DraftStateError(f"draft is {draft.get('status')}")
        draft.update(mutate(copy.deepcopy(draft)))
        return copy.deepcopy(draft)

    async def customer(self, merchant_id: str, customer_ref: str) -> dict[str, Any] | None:
        return self.customers.get(f"{merchant_id}:{customer_ref.lower()}")

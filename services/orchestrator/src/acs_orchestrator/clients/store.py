"""Firestore-backed operational records (free tier: 1 GiB, 50k reads / 20k writes per day).

Collections (all server-side only; firebase/firestore.rules denies client access):
  merchants/{merchant_id}                      storefront_domains, ucp settings
  api_clients/{client_id}                      hashed client credentials (see security/auth.py)
  po_intake/{intake_id}                        PO event dedupe ledger (id = merchant+sha256)
  a2a_tasks/{agent}:{task_id}                  tenant ownership of delegated A2A tasks
  refund_decisions/{merchant_id}:{idem_key}    idempotent refund decisions
  gmail_state/{mailbox}                        last processed Gmail historyId
  draft_sales_orders/{intake_id}               read-only here; written by the messy-order agent
  console_progress/{progress_id}               live progress of a slow console call (TTL)
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

PROGRESS_TTL = timedelta(hours=1)


def intake_id_for(merchant_id: str, sha256: str) -> str:
    return hashlib.sha256(f"{merchant_id}:{sha256}".encode()).hexdigest()[:40]


def _now() -> datetime:
    return datetime.now(UTC)


class Store(Protocol):
    async def storefront_domains(self, merchant_id: str) -> list[str]: ...
    async def claim_intake(self, intake_id: str, record: dict[str, Any]) -> bool: ...
    async def update_intake(self, intake_id: str, fields: dict[str, Any]) -> None: ...
    async def get_intake(self, intake_id: str) -> dict[str, Any] | None: ...
    async def record_task(self, agent: str, task_id: str, record: dict[str, Any]) -> None: ...
    async def get_task(self, agent: str, task_id: str) -> dict[str, Any] | None: ...
    async def get_refund_decision(self, key: str) -> dict[str, Any] | None: ...
    async def save_refund_decision(self, key: str, record: dict[str, Any]) -> bool: ...
    async def get_gmail_history(self, mailbox: str) -> str | None: ...
    async def set_gmail_history(self, mailbox: str, history_id: str) -> None: ...
    async def get_draft_order(self, intake_id: str) -> dict[str, Any] | None: ...
    async def list_draft_orders(self, merchant_id: str, limit: int) -> list[dict[str, Any]]: ...
    async def put_progress(self, progress_id: str, record: dict[str, Any]) -> None: ...
    async def get_progress(self, progress_id: str) -> dict[str, Any] | None: ...


class FirestoreStore:
    def __init__(self, db: Any) -> None:
        self._db = db

    def _doc(self, collection: str, doc_id: str) -> Any:
        return self._db.collection(collection).document(doc_id)

    async def _get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        snap = await self._doc(collection, doc_id).get()
        return snap.to_dict() if snap.exists else None

    async def _create(self, collection: str, doc_id: str, record: dict[str, Any]) -> bool:
        from google.api_core.exceptions import AlreadyExists

        try:
            await self._doc(collection, doc_id).create({**record, "created_at": _now()})
            return True
        except AlreadyExists:
            return False

    async def storefront_domains(self, merchant_id: str) -> list[str]:
        merchant = await self._get("merchants", merchant_id) or {}
        domains = merchant.get("storefront_domains", [])
        return [d for d in domains if isinstance(d, str)]

    async def claim_intake(self, intake_id: str, record: dict[str, Any]) -> bool:
        return await self._create("po_intake", intake_id, record)

    async def update_intake(self, intake_id: str, fields: dict[str, Any]) -> None:
        await self._doc("po_intake", intake_id).update({**fields, "updated_at": _now()})

    async def get_intake(self, intake_id: str) -> dict[str, Any] | None:
        return await self._get("po_intake", intake_id)

    async def record_task(self, agent: str, task_id: str, record: dict[str, Any]) -> None:
        await self._doc("a2a_tasks", f"{agent}:{task_id}").set({**record, "created_at": _now()})

    async def get_task(self, agent: str, task_id: str) -> dict[str, Any] | None:
        return await self._get("a2a_tasks", f"{agent}:{task_id}")

    async def get_refund_decision(self, key: str) -> dict[str, Any] | None:
        return await self._get("refund_decisions", key)

    async def save_refund_decision(self, key: str, record: dict[str, Any]) -> bool:
        return await self._create("refund_decisions", key, record)

    async def get_gmail_history(self, mailbox: str) -> str | None:
        record = await self._get("gmail_state", mailbox)
        return (record or {}).get("history_id")

    async def set_gmail_history(self, mailbox: str, history_id: str) -> None:
        await self._doc("gmail_state", mailbox).set(
            {"history_id": history_id, "updated_at": _now()}
        )

    async def get_draft_order(self, intake_id: str) -> dict[str, Any] | None:
        return await self._get("draft_sales_orders", intake_id)

    async def list_draft_orders(self, merchant_id: str, limit: int) -> list[dict[str, Any]]:
        from google.cloud.firestore_v1 import FieldFilter, Query

        # Composite index (merchant_id, created_at desc): infra/terraform/agents.tf.
        query = (
            self._db.collection("draft_sales_orders")
            .where(filter=FieldFilter("merchant_id", "==", merchant_id))
            .order_by("created_at", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [snap.to_dict() async for snap in query.stream()]

    async def put_progress(self, progress_id: str, record: dict[str, Any]) -> None:
        now = _now()
        await self._doc("console_progress", progress_id).set(
            {**record, "updated_at": now, "expire_at": now + PROGRESS_TTL}
        )

    async def get_progress(self, progress_id: str) -> dict[str, Any] | None:
        return await self._get("console_progress", progress_id)


class MemoryStore:
    """In-process store for local development and tests."""

    def __init__(self, merchants: dict[str, dict[str, Any]] | None = None) -> None:
        self.data: dict[str, dict[str, dict[str, Any]]] = {"merchants": dict(merchants or {})}

    def _col(self, name: str) -> dict[str, dict[str, Any]]:
        return self.data.setdefault(name, {})

    async def storefront_domains(self, merchant_id: str) -> list[str]:
        return list(self._col("merchants").get(merchant_id, {}).get("storefront_domains", []))

    async def claim_intake(self, intake_id: str, record: dict[str, Any]) -> bool:
        col = self._col("po_intake")
        if intake_id in col:
            return False
        col[intake_id] = {**record, "created_at": _now()}
        return True

    async def update_intake(self, intake_id: str, fields: dict[str, Any]) -> None:
        self._col("po_intake")[intake_id].update(fields)

    async def get_intake(self, intake_id: str) -> dict[str, Any] | None:
        return self._col("po_intake").get(intake_id)

    async def record_task(self, agent: str, task_id: str, record: dict[str, Any]) -> None:
        self._col("a2a_tasks")[f"{agent}:{task_id}"] = record

    async def get_task(self, agent: str, task_id: str) -> dict[str, Any] | None:
        return self._col("a2a_tasks").get(f"{agent}:{task_id}")

    async def get_refund_decision(self, key: str) -> dict[str, Any] | None:
        return self._col("refund_decisions").get(key)

    async def save_refund_decision(self, key: str, record: dict[str, Any]) -> bool:
        col = self._col("refund_decisions")
        if key in col:
            return False
        col[key] = record
        return True

    async def get_gmail_history(self, mailbox: str) -> str | None:
        return self._col("gmail_state").get(mailbox, {}).get("history_id")

    async def set_gmail_history(self, mailbox: str, history_id: str) -> None:
        self._col("gmail_state")[mailbox] = {"history_id": history_id}

    async def get_draft_order(self, intake_id: str) -> dict[str, Any] | None:
        return self._col("draft_sales_orders").get(intake_id)

    async def list_draft_orders(self, merchant_id: str, limit: int) -> list[dict[str, Any]]:
        drafts = [d for d in self._col("draft_sales_orders").values()
                  if d.get("merchant_id") == merchant_id]  # fmt: skip
        drafts.sort(key=lambda d: d.get("created_at") or _now(), reverse=True)
        return drafts[:limit]

    async def put_progress(self, progress_id: str, record: dict[str, Any]) -> None:
        self._col("console_progress")[progress_id] = {**record, "updated_at": _now()}

    async def get_progress(self, progress_id: str) -> dict[str, Any] | None:
        return self._col("console_progress").get(progress_id)

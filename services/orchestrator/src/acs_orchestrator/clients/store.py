"""Firestore-backed operational records (free tier: 1 GiB, 50k reads / 20k writes per day).

Collections (all server-side only; firebase/firestore.rules denies client access):
  merchants/{merchant_id}                      storefront_domains, ucp settings
  api_clients/{client_id}                      hashed client credentials (see security/auth.py)
  po_intake/{intake_id}                        PO event dedupe ledger (id = merchant+sha256)
  a2a_tasks/{agent}:{task_id}                  tenant ownership of delegated A2A tasks
  refund_decisions/{merchant_id}:{idem_key}    idempotent refund decisions
  gmail_state/{mailbox}                        last processed Gmail historyId
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Protocol


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

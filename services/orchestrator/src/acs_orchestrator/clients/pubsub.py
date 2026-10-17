"""Publisher for the PO intake topic - diagram edge "Publishes PO" -> Pub/Sub (PO Intake Events).

Message contract (docs/contracts/po-intake-event.md):
  data       = raw PO bytes (PDF / audio / UTF-8 text), at most ACS_MAX_PO_BYTES
  attributes = schema_version, intake_id, merchant_id, source, content_type,
               sha256, size, filename, received_at, submitted_by[, customer_ref]
The Messy-Order-to-ERP agent consumes it via a push subscription, stores the raw
PO in Cloud Storage and drafts the sales order.
"""

from __future__ import annotations

import asyncio
from typing import Protocol


class PoPublisher(Protocol):
    async def publish(self, data: bytes, attributes: dict[str, str]) -> str: ...


class PubSubPoPublisher:
    def __init__(self, project_id: str, topic: str) -> None:
        from google.cloud import pubsub_v1

        self._client = pubsub_v1.PublisherClient()
        self._topic_path = self._client.topic_path(project_id, topic)

    async def publish(self, data: bytes, attributes: dict[str, str]) -> str:
        future = self._client.publish(self._topic_path, data, **attributes)
        return await asyncio.wrap_future(future)


class MemoryPoPublisher:
    """Local-dev / test publisher."""

    def __init__(self) -> None:
        self.messages: list[tuple[bytes, dict[str, str]]] = []

    async def publish(self, data: bytes, attributes: dict[str, str]) -> str:
        self.messages.append((data, attributes))
        return str(len(self.messages))

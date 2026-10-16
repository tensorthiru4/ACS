"""SKU matching - diagram edge "Vector SKU Match" (Firestore KNN on `skus.embedding`).

Catalog document `skus/{merchant_id}:{sku}` (loaded with `acs-messy-admin load-skus`):
  merchant_id, sku, sku_key (normalized), alias_keys[], name, description, uom,
  list_price_minor, currency, odoo_product_id, embedding (Vector, 768 dims)

A line is matched by, in order:
  1. exact code: the buyer's SKU equals a catalog SKU or alias;
  2. vector: the line text's embedding is the nearest catalog item within
     `max_distance` (cosine) and clearly ahead of the runner-up.
Anything else stays unmatched and sends the draft to human review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from acs_common.gemini import ModelError, embed

from .models import ExtractedLine, TransientError

_KEY = re.compile(r"[^A-Z0-9]+")


def sku_key(code: str) -> str:
    return _KEY.sub("", code.upper())[:64]


@dataclass(frozen=True)
class SkuMatch:
    sku: str
    name: str
    odoo_product_id: int | None
    list_price_minor: int | None
    currency: str
    uom: str
    method: str  # exact | vector | manual
    confidence: float

    @classmethod
    def from_doc(cls, doc: dict[str, Any], method: str, confidence: float) -> SkuMatch:
        product = doc.get("odoo_product_id")
        price = doc.get("list_price_minor")
        return cls(
            sku=str(doc.get("sku", "")),
            name=str(doc.get("name", ""))[:200],
            odoo_product_id=product if isinstance(product, int) and product > 0 else None,
            list_price_minor=price if isinstance(price, int) and price >= 0 else None,
            currency=str(doc.get("currency", "")),
            uom=str(doc.get("uom", "")),
            method=method,
            confidence=round(confidence, 4),
        )


class Catalog(Protocol):
    async def by_code(self, merchant_id: str, key: str) -> dict[str, Any] | None: ...
    async def nearest(
        self, merchant_id: str, vector: list[float], limit: int
    ) -> list[tuple[dict[str, Any], float]]: ...


class FirestoreCatalog:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def by_code(self, merchant_id: str, key: str) -> dict[str, Any] | None:
        from google.cloud.firestore_v1 import FieldFilter

        skus = self._db.collection("skus")
        mine = skus.where(filter=FieldFilter("merchant_id", "==", merchant_id))
        for query in (
            mine.where(filter=FieldFilter("sku_key", "==", key)),
            mine.where(filter=FieldFilter("alias_keys", "array_contains", key)),
        ):
            async for snap in query.limit(1).stream():
                return snap.to_dict()
        return None

    async def nearest(
        self, merchant_id: str, vector: list[float], limit: int
    ) -> list[tuple[dict[str, Any], float]]:
        from google.cloud.firestore_v1 import FieldFilter
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector

        query = (
            self._db.collection("skus")
            .where(filter=FieldFilter("merchant_id", "==", merchant_id))
            .find_nearest(
                vector_field="embedding",
                query_vector=Vector(vector),
                distance_measure=DistanceMeasure.COSINE,
                limit=limit,
                distance_result_field="vector_distance",
            )
        )
        out: list[tuple[dict[str, Any], float]] = []
        async for snap in query.stream():
            doc = snap.to_dict()
            out.append((doc, float(doc.pop("vector_distance", 1.0))))
        return out


class MemoryCatalog:
    """Local/test catalog: exact codes plus caller-supplied nearest-neighbour results."""

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = list(docs or [])
        self.neighbours: dict[str, list[tuple[str, float]]] = {}

    async def by_code(self, merchant_id: str, key: str) -> dict[str, Any] | None:
        for doc in self.docs:
            if doc["merchant_id"] == merchant_id and (
                doc.get("sku_key") == key or key in doc.get("alias_keys", [])
            ):
                return doc
        return None

    async def nearest(
        self, merchant_id: str, vector: list[float], limit: int
    ) -> list[tuple[dict[str, Any], float]]:
        # Tests encode the query text's hash into vector[0]; map it back to fixtures.
        hits = self.neighbours.get(str(vector[0]), [])
        by_sku = {d["sku"]: d for d in self.docs if d["merchant_id"] == merchant_id}
        return [(by_sku[s], d) for s, d in hits if s in by_sku][:limit]


def line_query(line: ExtractedLine) -> str:
    text = line.description
    if line.customer_sku:
        text += f" (buyer code {line.customer_sku})"
    if line.uom:
        text += f" [{line.uom}]"
    return text[:600]


class SkuMatcher:
    def __init__(
        self,
        catalog: Catalog,
        *,
        client: Any,
        model: str,
        dimensions: int,
        max_distance: float,
        min_margin: float,
    ) -> None:
        self._catalog = catalog
        self._client = client
        self._model = model
        self._dims = dimensions
        self._max_distance = max_distance
        self._min_margin = min_margin

    async def by_sku(self, merchant_id: str, sku: str) -> SkuMatch | None:
        doc = await self._catalog.by_code(merchant_id, sku_key(sku))
        return SkuMatch.from_doc(doc, "manual", 1.0) if doc else None

    async def match(self, merchant_id: str, lines: list[ExtractedLine]) -> list[SkuMatch | None]:
        matches: list[SkuMatch | None] = [None] * len(lines)
        pending: list[int] = []
        for i, line in enumerate(lines):
            key = sku_key(line.customer_sku or "")
            doc = await self._catalog.by_code(merchant_id, key) if key else None
            if doc:
                matches[i] = SkuMatch.from_doc(doc, "exact", 1.0)
            else:
                pending.append(i)
        if not pending:
            return matches
        try:
            vectors = await embed(
                self._client,
                self._model,
                [line_query(lines[i]) for i in pending],
                task_type="RETRIEVAL_QUERY",
                dimensions=self._dims,
            )
        except ModelError as exc:
            raise TransientError(f"embedding failed: {exc}") from exc
        for i, vector in zip(pending, vectors, strict=True):
            hits = await self._catalog.nearest(merchant_id, vector, 2)
            if not hits:
                continue
            best_doc, best = hits[0]
            runner_up = hits[1][1] if len(hits) > 1 else 1.0
            if best <= self._max_distance and runner_up - best >= self._min_margin:
                matches[i] = SkuMatch.from_doc(best_doc, "vector", max(0.0, 1.0 - best))
        return matches

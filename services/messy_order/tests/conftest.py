from __future__ import annotations

import base64
import hashlib
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from acs_messy_order.adapters import MemoryErpLauncher, MemoryRawPoStore, TermsChecker
from acs_messy_order.catalog import MemoryCatalog, SkuMatcher, sku_key
from acs_messy_order.config import Settings
from acs_messy_order.extraction import Extractor
from acs_messy_order.main import create_app
from acs_messy_order.pipeline import Pipeline
from acs_messy_order.repository import MemoryRepository

PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer\n%%EOF\n"
INTAKE_ID = "a" * 40
DIMS = 768


class FakeGemini:
    """generate_content plays back extraction outputs; embed_content maps each text to a
    vector whose first component is a per-text id, so MemoryCatalog can look it up."""

    def __init__(self) -> None:
        self.extractions: list[Any] = []
        self.generate_calls: list[dict[str, Any]] = []
        self.embed_ids: dict[str, float] = {}
        self.aio = SimpleNamespace(
            models=SimpleNamespace(generate_content=self._generate, embed_content=self._embed)
        )

    async def _generate(self, *, model: str, contents: Any, config: Any) -> Any:
        self.generate_calls.append({"model": model, "contents": contents, "config": config})
        step = self.extractions.pop(0)
        if isinstance(step, Exception):
            raise step
        return SimpleNamespace(text=json.dumps(step))

    async def _embed(self, *, model: str, contents: list[str], config: Any) -> Any:
        vectors = []
        for text in contents:
            ident = self.embed_ids.setdefault(text, float(len(self.embed_ids) + 1))
            vectors.append(SimpleNamespace(values=[ident] + [0.0] * (DIMS - 1)))
        return SimpleNamespace(embeddings=vectors)


def sku(code: str, name: str, price_minor: int, product: int | None, aliases=()) -> dict:
    return {
        "merchant_id": "m1",
        "sku": code,
        "sku_key": sku_key(code),
        "alias_keys": [sku_key(a) for a in aliases],
        "name": name,
        "uom": "ea",
        "list_price_minor": price_minor,
        "currency": "USD",
        "odoo_product_id": product,
    }


def extraction(**overrides) -> dict:
    value = {
        "is_purchase_order": True,
        "po_number": "PO-778",
        "buyer_name": "Buyer One",
        "buyer_reference": None,
        "order_date": "2026-10-01",
        "requested_delivery_date": None,
        "currency": "USD",
        "payment_terms_days": 30,
        "ship_to": "1 Main St",
        "lines": [
            {
                "description": "Blue widget",
                "customer_sku": "BW-1",
                "quantity": 10,
                "uom": "ea",
                "unit_price": 12.5,
            },
            {
                "description": "Large red gadget",
                "customer_sku": None,
                "quantity": 2,
                "uom": "ea",
                "unit_price": None,
            },
        ],  # fmt: skip
        "notes": None,
        "confidence": 0.93,
        "warnings": [],
    }
    value.update(overrides)
    return value


def push_body(data: bytes = PDF, *, attempt: int = 1, **attrs: str) -> dict:
    attributes = {
        "schema_version": "1",
        "intake_id": INTAKE_ID,
        "merchant_id": "m1",
        "source": "upload",
        "content_type": "application/pdf",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": str(len(data)),
        "filename": "po.pdf",
        "received_at": "2026-10-16T10:00:00+00:00",
        "submitted_by": "m1:b2b_partner:partner1",
        "customer_ref": "buyer-one.example",
    }
    attributes.update(attrs)
    return {
        "message": {
            "data": base64.b64encode(data).decode(),
            "attributes": attributes,
            "messageId": "msg-1",
        },
        "subscription": "projects/p/subscriptions/po-intake-events-messy-order",
        "deliveryAttempt": attempt,
    }


@pytest.fixture
def h():
    class Harness:
        pass

    h = Harness()
    h.settings = Settings(env="local", max_delivery_attempts=3)
    h.gemini = FakeGemini()
    h.repo = MemoryRepository()
    h.repo.customers["m1:buyer-one.example"] = {
        "odoo_partner_id": 42,
        "name": "Buyer One",
        "payment_terms_days": 30,
    }
    h.repo.intake[INTAKE_ID] = {"merchant_id": "m1", "status": "queued"}
    h.catalog = MemoryCatalog(
        [
            sku("WID-BLUE", "Blue widget", 1250, 101, aliases=["BW-1"]),
            sku("GAD-RED-L", "Red gadget, large", 4000, 102),
            sku("GAD-RED-S", "Red gadget, small", 3000, 103),
        ]
    )
    h.raw = MemoryRawPoStore()
    h.erp = MemoryErpLauncher()
    h.rule_calls: list[dict] = []
    h.rule_response = {
        "decision": "APPROVE",
        "reasons": ["known customer, small order"],
        "matched_rule_ids": ["small_orders"],
        "rule_set_version": "v3",
    }
    h.rule_status = 200

    def rules(request: httpx.Request) -> httpx.Response:
        h.rule_calls.append(json.loads(request.content))
        return httpx.Response(h.rule_status, json=h.rule_response)

    h.pipeline = Pipeline(
        settings=h.settings,
        repo=h.repo,
        raw_store=h.raw,
        extractor=Extractor(h.gemini, "gemini-test"),
        matcher=SkuMatcher(
            h.catalog,
            client=h.gemini,
            model="embed-test",
            dimensions=DIMS,
            max_distance=0.2,
            min_margin=0.02,
        ),
        terms=TermsChecker(
            httpx.AsyncClient(transport=httpx.MockTransport(rules), base_url="http://rules")
        ),
        erp=h.erp,
    )

    def near(text: str, *hits: tuple[str, float]) -> None:
        ident = h.gemini.embed_ids.setdefault(text, float(len(h.gemini.embed_ids) + 1))
        h.catalog.neighbours[str(ident)] = list(hits)

    h.near = near
    # The second line of `extraction()` matches GAD-RED-L by vector, clearly ahead.
    near("Large red gadget [ea]", ("GAD-RED-L", 0.08), ("GAD-RED-S", 0.15))
    with TestClient(create_app(h.settings, h.pipeline)) as client:
        h.client = client
        yield h

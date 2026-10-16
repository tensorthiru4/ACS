"""PO intake -> draft sales order.

    Pub/Sub push ─► lease intake ─► store raw PO (GCS) ─► Gemini extraction
                 ─► SKU match (exact, then vector) ─► customer mapping
                 ─► rule engine `order_terms` ─► draft SO (Firestore)
                 ─► APPROVE: start ERP bridge workflow │ ESCALATE: human review │ DENY: rejected

A human releases or rejects `needs_review` drafts from the Merchant Console (through
the orchestrator). The ERP bridge re-checks every invariant before posting to Odoo.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Any

from acs_common.logging_setup import audit

from .adapters import ErpLauncher, RawPoStore, TermsChecker
from .catalog import SkuMatch, SkuMatcher
from .config import Settings
from .extraction import Extractor
from .models import Extraction, PermanentError, PoEvent, currency_exponent, to_minor
from .repository import REJECTABLE, RELEASABLE, DraftStateError, Repository

logger = logging.getLogger(__name__)

STATUS_FOR_DECISION = {"APPROVE": "approved", "ESCALATE": "needs_review", "DENY": "rejected"}


class IntakeBusy(RuntimeError):
    """Another delivery holds the lease; Pub/Sub should retry later."""


class InvalidOverride(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


def priced_line(line: dict[str, Any], match: SkuMatch | None, currency: str) -> dict[str, Any]:
    """Attach a catalog match and price to a line; recomputes every derived field."""
    quoted = line.get("quoted_price_minor")
    usable = match is not None and match.currency in ("", currency)
    list_price = match.list_price_minor if usable and match else None
    price = quoted if quoted is not None else list_price
    variance = 0.0
    if quoted is not None and list_price:
        variance = round(abs(quoted - list_price) * 100.0 / list_price, 2)
    elif quoted is not None and list_price == 0 and quoted > 0:
        variance = 100.0
    complete = bool(usable and match and match.odoo_product_id and match.sku and price is not None)
    return {
        **line,
        "sku": match.sku if match else None,
        "product_name": match.name if match else None,
        "odoo_product_id": match.odoo_product_id if match else None,
        "match_method": match.method if match else "none",
        "match_confidence": match.confidence if match else 0.0,
        "list_price_minor": list_price,
        "unit_price_minor": price,
        "unit_price": None if price is None else price / 10 ** currency_exponent(currency),
        "price_variance_pct": variance,
        "currency_mismatch": bool(match and not usable),
        "complete": complete,
    }


def line_total(line: dict[str, Any]) -> int:
    price = line.get("unit_price_minor")
    return round(float(line["quantity"]) * price) if price is not None else 0


def _days_until(value: str | None) -> int | None:
    try:
        return (date.fromisoformat(value or "") - _now().date()).days
    except ValueError:
        return None


def terms_facts(draft: dict[str, Any], extraction_confidence: float, source: str) -> dict[str, Any]:
    lines = draft["lines"]
    customer = draft["customer"]
    variances = [ln["price_variance_pct"] for ln in lines]
    return {
        "order_total_minor": draft["total_minor"],
        "currency": draft["currency"],
        "line_count": len(lines),
        "unmatched_line_count": sum(not ln["complete"] for ln in lines),
        "min_match_confidence": min((ln["match_confidence"] for ln in lines), default=0.0),
        "max_price_variance_pct": max(variances, default=0.0),
        "customer_known": customer["known"],
        "customer_ref": customer["ref"],
        "payment_terms_days": draft.get("payment_terms_days"),
        "customer_payment_terms_days": customer.get("payment_terms_days"),
        "extraction_confidence": extraction_confidence,
        "source": source,
        "has_po_number": bool(draft.get("po_number")),
        "requested_delivery_days": _days_until(draft.get("requested_delivery_date")),
    }


def customer_view(ref: str, record: dict[str, Any] | None) -> dict[str, Any]:
    partner = (record or {}).get("odoo_partner_id")
    terms = (record or {}).get("payment_terms_days")
    known = isinstance(partner, int) and partner > 0
    return {
        "ref": ref,
        "known": known,
        "odoo_partner_id": partner if known else None,
        "name": str((record or {}).get("name", ""))[:200],
        "payment_terms_days": terms if isinstance(terms, int) else None,
    }


class Pipeline:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: Repository,
        raw_store: RawPoStore,
        extractor: Extractor,
        matcher: SkuMatcher,
        terms: TermsChecker,
        erp: ErpLauncher | None,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.raw_store = raw_store
        self.extractor = extractor
        self.matcher = matcher
        self.terms = terms
        self.erp = erp

    # ================================================================ push consumer
    async def handle(self, event: PoEvent) -> str:
        a = event.attributes
        lease = await self.repo.lease_intake(
            a.intake_id, a.merchant_id, event.message_id, self.settings.lease_s
        )
        if lease == "done":
            return "duplicate"
        if lease == "mismatch":
            audit("po.intake", principal="pubsub", outcome="tenant_mismatch", intake_id=a.intake_id)
            return "rejected"
        if lease == "busy":
            raise IntakeBusy(a.intake_id)
        try:
            draft = await self.repo.get_draft(a.intake_id)
            if draft is None:
                draft = await self._draft(event)
                if not await self.repo.create_draft(draft):
                    draft = await self.repo.get_draft(a.intake_id) or draft
            draft = await self._ensure_erp_started(draft)
            await self.repo.finish_intake(
                a.intake_id, "drafted", {"draft_id": a.intake_id, "draft_status": draft["status"]}
            )
        except PermanentError as exc:
            await self.repo.finish_intake(a.intake_id, "failed", {"error": str(exc)[:300]})
            audit("po.draft", principal="pubsub", outcome="failed", intake_id=a.intake_id,
                  error=str(exc)[:200])  # fmt: skip
            return "failed"
        except Exception as exc:
            if event.delivery_attempt >= self.settings.max_delivery_attempts:
                await self.repo.finish_intake(
                    a.intake_id,
                    "failed",
                    {"error": f"gave up after {event.delivery_attempt} attempts"},
                )
            else:
                await self.repo.release_intake(a.intake_id, type(exc).__name__)
            raise
        audit(
            "po.draft",
            principal="pubsub",
            outcome=draft["status"],
            intake_id=a.intake_id,
            merchant_id=a.merchant_id,
            total_minor=draft["total_minor"],
            lines=len(draft["lines"]),
            rule_set_version=draft["terms"]["rule_set_version"],
        )
        return draft["status"]

    async def _draft(self, event: PoEvent) -> dict[str, Any]:
        a = event.attributes
        raw_uri = await self.raw_store.put(a.merchant_id, a.intake_id, event.data, a.content_type)
        extraction = await self.extractor.extract(
            event.data, a.content_type, filename=a.filename or a.intake_id
        )
        if not extraction.is_purchase_order:
            raise PermanentError("document is not a purchase order")
        if not extraction.lines:
            raise PermanentError("no order lines found")
        return await self.build_draft(event, extraction, raw_uri)

    async def build_draft(
        self, event: PoEvent, extraction: Extraction, raw_uri: str
    ) -> dict[str, Any]:
        a = event.attributes
        currency = extraction.currency or self.settings.default_currency
        matches = await self.matcher.match(a.merchant_id, extraction.lines)
        lines = [
            priced_line(
                {
                    "line_no": n,
                    "description": line.description,
                    "customer_sku": line.customer_sku,
                    "quantity": line.quantity,
                    "uom": line.uom,
                    "quoted_price_minor": to_minor(line.unit_price, currency),
                },
                match,
                currency,
            )
            for n, (line, match) in enumerate(zip(extraction.lines, matches, strict=True), 1)
        ]
        # Identity comes from the authenticated channel, never from the document text.
        ref = a.customer_ref.lower()
        customer = customer_view(ref, await self.repo.customer(a.merchant_id, ref) if ref else None)
        draft: dict[str, Any] = {
            "intake_id": a.intake_id,
            "merchant_id": a.merchant_id,
            "source": a.source,
            "submitted_by": a.submitted_by,
            "raw_po_uri": raw_uri,
            "content_type": a.content_type,
            "po_number": extraction.po_number,
            "order_date": extraction.order_date,
            "requested_delivery_date": extraction.requested_delivery_date,
            "payment_terms_days": extraction.payment_terms_days,
            "currency": currency,
            "customer": customer,
            "lines": lines,
            "total_minor": sum(line_total(ln) for ln in lines),
            "extraction": {
                "confidence": extraction.confidence,
                "warnings": extraction.warnings,
                "buyer_name": extraction.buyer_name,
                "buyer_reference": extraction.buyer_reference,
                "ship_to": extraction.ship_to,
                "notes": extraction.notes,
                "model": self.settings.gemini_model,
            },
        }
        facts = terms_facts(draft, extraction.confidence, a.source)
        verdict = await self.terms.check(a.merchant_id, facts)
        draft.update(
            facts=facts,
            terms={
                "decision": verdict.decision,
                "reasons": verdict.reasons,
                "matched_rule_ids": verdict.matched_rule_ids,
                "rule_set_version": verdict.rule_set_version,
                "source": verdict.source,
            },
            status=STATUS_FOR_DECISION[verdict.decision],
            approved_by="rules:order_terms" if verdict.decision == "APPROVE" else None,
        )
        return draft

    async def _ensure_erp_started(self, draft: dict[str, Any]) -> dict[str, Any]:
        if draft["status"] != "approved" or (draft.get("erp") or {}).get("execution"):
            return draft
        if self.erp is None:
            logger.warning(
                "ERP workflow not configured; draft %s stays approved", draft["intake_id"]
            )
            return draft
        execution = await self.erp.start(draft["merchant_id"], draft["intake_id"])
        erp = {"execution": execution, "status": "started", "started_at": _now()}
        await self.repo.update_draft(draft["intake_id"], {"erp": erp})
        return {**draft, "erp": erp}

    # ================================================================ human review
    async def release(
        self,
        intake_id: str,
        *,
        merchant_id: str,
        approved_by: str,
        line_overrides: dict[int, str],
        customer_ref: str | None,
    ) -> dict[str, Any]:
        resolved: dict[int, SkuMatch] = {}
        for line_no, sku in line_overrides.items():
            match = await self.matcher.by_sku(merchant_id, sku)
            if match is None:
                raise InvalidOverride(f"line {line_no}: unknown SKU")
            resolved[line_no] = match
        customer = None
        if customer_ref:
            ref = customer_ref.lower()
            customer = customer_view(ref, await self.repo.customer(merchant_id, ref))
            if not customer["known"]:
                raise InvalidOverride("customer_ref is not mapped to an ERP customer")

        def mutate(draft: dict[str, Any]) -> dict[str, Any]:
            known_lines = {ln["line_no"] for ln in draft["lines"]}
            if set(resolved) - known_lines:
                raise InvalidOverride("override for a line that does not exist")
            lines = [
                priced_line(ln, resolved[ln["line_no"]], draft["currency"])
                if ln["line_no"] in resolved
                else ln
                for ln in draft["lines"]
            ]
            cust = customer or draft["customer"]
            problems = [f"line {ln['line_no']} has no ERP product or price" for ln in lines
                        if not ln["complete"]]  # fmt: skip
            if not cust["known"]:
                problems.append("customer is not mapped to an ERP customer")
            if problems:
                raise DraftStateError("draft is incomplete: " + "; ".join(problems[:5]))
            return {
                "lines": lines,
                "customer": cust,
                "total_minor": sum(line_total(ln) for ln in lines),
                "status": "approved",
                "approved_by": approved_by,
                "approved_at": _now(),
                # A re-release after an ERP failure needs a fresh workflow execution.
                "erp": {"previous": draft.get("erp")} if draft.get("erp") else None,
            }

        draft = await self.repo.transition_draft(intake_id, merchant_id, RELEASABLE, mutate)
        draft = await self._ensure_erp_started(draft)
        audit("po.release", principal=approved_by, outcome="approved", intake_id=intake_id,
              merchant_id=merchant_id, overrides=len(resolved))  # fmt: skip
        return draft

    async def reject(
        self, intake_id: str, *, merchant_id: str, rejected_by: str, reason: str
    ) -> dict[str, Any]:
        def mutate(draft: dict[str, Any]) -> dict[str, Any]:
            if (draft.get("erp") or {}).get("status") == "posting":
                raise DraftStateError("draft is being posted to the ERP")
            return {"status": "rejected", "rejected_by": rejected_by, "rejected_at": _now(),
                    "rejection_reason": reason}  # fmt: skip

        draft = await self.repo.transition_draft(intake_id, merchant_id, REJECTABLE, mutate)
        audit("po.reject", principal=rejected_by, outcome="rejected", intake_id=intake_id,
              merchant_id=merchant_id)  # fmt: skip
        return draft

"""Rule-set loading, caching and evaluation - diagram edge "Reads Rule Sets" (Firestore).

The engine never calls an LLM. A missing rule set is evaluated as an empty one (only
the baseline guards, default ESCALATE); a stored rule set that no longer validates
is treated the same way and logged, so a bad document can never approve anything.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Protocol

from acs_common.rules import (
    CompiledRuleSet,
    Evaluation,
    RuleSet,
    RuleValidationError,
    compile_rule_set,
    evaluate,
    normalize_facts,
)
from cachetools import TTLCache
from pydantic import ValidationError

logger = logging.getLogger(__name__)


def rule_set_doc_id(merchant_id: str, rule_set: str) -> str:
    return f"{merchant_id}:{rule_set}"


class RuleSetSource(Protocol):
    async def get(self, merchant_id: str, rule_set: str) -> dict[str, Any] | None: ...


class FirestoreRuleSets:
    def __init__(self, db: Any) -> None:
        self._db = db

    async def get(self, merchant_id: str, rule_set: str) -> dict[str, Any] | None:
        snap = (
            await self._db.collection("rule_sets")
            .document(rule_set_doc_id(merchant_id, rule_set))
            .get()
        )
        return snap.to_dict() if snap.exists else None


class MemoryRuleSets:
    def __init__(self, docs: dict[str, dict[str, Any]] | None = None) -> None:
        self.docs = dict(docs or {})

    @classmethod
    def from_file(cls, path: str) -> MemoryRuleSets:
        docs = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls({rule_set_doc_id(d["merchant_id"], d["rule_set"]): d for d in docs})

    async def get(self, merchant_id: str, rule_set: str) -> dict[str, Any] | None:
        return self.docs.get(rule_set_doc_id(merchant_id, rule_set))


class RuleEngine:
    def __init__(self, source: RuleSetSource, *, cache_ttl_s: int = 30) -> None:
        self._source = source
        self._cache: TTLCache[str, CompiledRuleSet] = TTLCache(maxsize=512, ttl=cache_ttl_s)

    async def compiled(self, merchant_id: str, rule_set: str) -> CompiledRuleSet:
        key = rule_set_doc_id(merchant_id, rule_set)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        doc = await self._source.get(merchant_id, rule_set)
        empty = RuleSet(merchant_id=merchant_id, rule_set=rule_set, version="")  # type: ignore[arg-type]
        if doc is None:
            compiled = compile_rule_set(empty)
        else:
            try:
                stored = RuleSet.model_validate({**doc, "merchant_id": merchant_id})
                if stored.rule_set != rule_set:
                    raise ValueError("rule_set field does not match the document id")
                compiled = compile_rule_set(stored)
            except (ValidationError, RuleValidationError, ValueError) as exc:
                logger.error("stored rule set %s is invalid; serving baseline only: %s", key, exc)
                compiled = compile_rule_set(
                    empty.model_copy(update={"version": f"{doc.get('version', '')}!invalid"})
                )
        self._cache[key] = compiled
        return compiled

    async def evaluate(
        self, merchant_id: str, rule_set: str, facts: dict[str, Any]
    ) -> tuple[Evaluation, str]:
        normalized = normalize_facts(rule_set, facts)
        compiled = await self.compiled(merchant_id, rule_set)
        return evaluate(compiled, normalized), compiled.rule_set.version

"""Draft and rule-set persistence - diagram edge "Deploys Rules".

Collections:
  policy_drafts/{draft_id}                         compiled drafts awaiting approval (TTL)
  rule_sets/{merchant_id}:{rule_set}               live rule set read by the rule engine
  rule_sets/{merchant_id}:{rule_set}/versions/{v}  immutable history of every deploy

Deploys are transactional with optimistic concurrency: the caller names the version
it expects to replace, and the deploy is rejected if anything else was deployed since.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from acs_common.rules import RuleSet, compile_rule_set

INITIAL_VERSION = "v0"
_VERSION = re.compile(r"^v(\d{1,9})$")


class DraftError(LookupError):
    """Draft missing, owned by another merchant, expired or already deployed."""


class VersionConflict(RuntimeError):
    def __init__(self, current: str, expected: str) -> None:
        super().__init__(f"rule set is at {current}, not {expected}; recompile the policy")
        self.current = current


class ApproverError(PermissionError):
    pass


def next_version(current: str) -> str:
    match = _VERSION.match(current)
    return f"v{int(match.group(1)) + 1}" if match else "v1"


def rule_set_id(merchant_id: str, rule_set: str) -> str:
    return f"{merchant_id}:{rule_set}"


def _now() -> datetime:
    return datetime.now(UTC)


def build_live_doc(draft: dict[str, Any], version: str, approved_by: str) -> dict[str, Any]:
    body = draft["rule_set_doc"]
    # Defense in depth: never deploy something that does not validate today.
    compile_rule_set(
        RuleSet(
            merchant_id=draft["merchant_id"],
            rule_set=draft["rule_set"],
            version=version,
            default_decision=body["default_decision"],
            params=body.get("params", {}),
            rules=body["rules"],
        )
    )
    return {
        "merchant_id": draft["merchant_id"],
        "rule_set": draft["rule_set"],
        "version": version,
        "default_decision": body["default_decision"],
        "params": body.get("params", {}),
        "rules": body["rules"],
        "policy_name": draft["policy_name"],
        "draft_id": draft["draft_id"],
        "deployed_by": approved_by,
        "deployed_at": _now(),
    }


def check_draft(
    draft: dict[str, Any] | None, merchant_id: str, approved_by: str, distinct: bool
) -> dict[str, Any]:
    if not draft or draft.get("merchant_id") != merchant_id:
        raise DraftError("draft not found")
    if draft.get("status") != "draft":
        raise DraftError(f"draft is {draft.get('status')}, not deployable")
    expire_at = draft.get("expire_at")
    if isinstance(expire_at, datetime) and expire_at < _now():
        raise DraftError("draft expired; recompile the policy")
    if distinct and draft.get("requested_by") == approved_by:
        raise ApproverError("a different admin must approve this draft")
    return draft


class PolicyRepository(Protocol):
    async def current_version(self, merchant_id: str, rule_set: str) -> str: ...
    async def save_draft(self, draft: dict[str, Any]) -> None: ...
    async def deploy(
        self, merchant_id: str, draft_id: str, expected_version: str | None, approved_by: str
    ) -> dict[str, Any]: ...


class FirestorePolicyRepository:
    def __init__(self, db: Any, *, draft_ttl_days: int, distinct_approver: bool) -> None:
        self._db = db
        self._ttl = timedelta(days=draft_ttl_days)
        self._distinct = distinct_approver

    def _rule_set(self, merchant_id: str, rule_set: str) -> Any:
        return self._db.collection("rule_sets").document(rule_set_id(merchant_id, rule_set))

    async def current_version(self, merchant_id: str, rule_set: str) -> str:
        snap = await self._rule_set(merchant_id, rule_set).get()
        return (
            (snap.to_dict() or {}).get("version", INITIAL_VERSION)
            if snap.exists
            else (INITIAL_VERSION)
        )

    async def save_draft(self, draft: dict[str, Any]) -> None:
        now = _now()
        record = {**draft, "created_at": now, "expire_at": now + self._ttl}
        await self._db.collection("policy_drafts").document(draft["draft_id"]).create(record)

    async def deploy(
        self, merchant_id: str, draft_id: str, expected_version: str | None, approved_by: str
    ) -> dict[str, Any]:
        from google.cloud import firestore

        draft_ref = self._db.collection("policy_drafts").document(draft_id)

        @firestore.async_transactional
        async def run(transaction: Any) -> dict[str, Any]:
            draft_snap = await draft_ref.get(transaction=transaction)
            draft = check_draft(
                draft_snap.to_dict() if draft_snap.exists else None,
                merchant_id,
                approved_by,
                self._distinct,
            )
            live_ref = self._rule_set(merchant_id, draft["rule_set"])
            live_snap = await live_ref.get(transaction=transaction)
            current = (
                (live_snap.to_dict() or {}).get("version", INITIAL_VERSION)
                if live_snap.exists
                else INITIAL_VERSION
            )
            expected = expected_version or draft["base_version"]
            if current != expected:
                raise VersionConflict(current, expected)
            version = next_version(current)
            doc = build_live_doc(draft, version, approved_by)
            transaction.set(live_ref, doc)
            transaction.set(live_ref.collection("versions").document(version), doc)
            transaction.update(
                draft_ref,
                {
                    "status": "deployed",
                    "deployed_version": version,
                    "deployed_by": approved_by,
                    "deployed_at": doc["deployed_at"],
                },
            )
            return {**doc, "previous_version": current}

        return await run(self._db.transaction())


class MemoryPolicyRepository:
    """In-process repository for local development and tests (same semantics)."""

    def __init__(self, *, draft_ttl_days: int = 30, distinct_approver: bool = False) -> None:
        self.drafts: dict[str, dict[str, Any]] = {}
        self.rule_sets: dict[str, dict[str, Any]] = {}
        self.history: dict[str, list[dict[str, Any]]] = {}
        self._ttl = timedelta(days=draft_ttl_days)
        self._distinct = distinct_approver

    async def current_version(self, merchant_id: str, rule_set: str) -> str:
        return self.rule_sets.get(rule_set_id(merchant_id, rule_set), {}).get(
            "version", INITIAL_VERSION
        )

    async def save_draft(self, draft: dict[str, Any]) -> None:
        if draft["draft_id"] in self.drafts:
            raise DraftError("draft id collision")
        now = _now()
        self.drafts[draft["draft_id"]] = {
            **copy.deepcopy(draft),
            "created_at": now,
            "expire_at": now + self._ttl,
        }

    async def deploy(
        self, merchant_id: str, draft_id: str, expected_version: str | None, approved_by: str
    ) -> dict[str, Any]:
        draft = check_draft(self.drafts.get(draft_id), merchant_id, approved_by, self._distinct)
        key = rule_set_id(merchant_id, draft["rule_set"])
        current = self.rule_sets.get(key, {}).get("version", INITIAL_VERSION)
        expected = expected_version or draft["base_version"]
        if current != expected:
            raise VersionConflict(current, expected)
        version = next_version(current)
        doc = build_live_doc(draft, version, approved_by)
        self.rule_sets[key] = doc
        self.history.setdefault(key, []).append(doc)
        draft.update(status="deployed", deployed_version=version, deployed_by=approved_by)
        return {**doc, "previous_version": current}

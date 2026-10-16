"""Rule sets as code: schema, CEL validation and deterministic evaluation.

Used by the Deterministic Rule Engine (evaluation) and by the Policy-as-Code
Compiler (validating Gemini's drafts before anything is stored), so a draft that
validates here is evaluated identically once deployed.

Rule set document (Firestore `rule_sets/{merchant_id}:{rule_set}`):

    { "merchant_id": "m1", "rule_set": "refunds", "version": "v7",
      "default_decision": "ESCALATE",
      "params": { "unverified_escalation_minor": 5000 },
      "rules": [ { "id": "damaged_recent", "description": "...",
                   "when": "reason == 'damaged' && days_since_delivery <= 30",
                   "decision": "APPROVE", "reason": "...", "max_refund_minor": 20000 } ] }

Evaluation semantics (fail safe):

* Facts are typed per rule set (`FACT_SCHEMAS`); unknown keys are dropped and
  absent facts are bound to null, so a rule touching a missing fact errors.
* Every rule is evaluated. A rule that errors or yields a non-bool:
  - DENY / ESCALATE rule -> forces ESCALATE (a guard we could not evaluate);
  - APPROVE rule -> simply does not match.
* Precedence: DENY > ESCALATE > APPROVE > `default_decision` (ESCALATE unless the
  rule set says otherwise). APPROVE caps the refund at the smallest
  `max_refund_minor` among the matching APPROVE rules.
* Baseline guard rules (`sys_*`) are enforced by the engine for every merchant and
  cannot be removed by a rule set; their thresholds are tunable via `params`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

import cel
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_RULES = 100
MAX_EXPRESSION_CHARS = 1000
MAX_STRING_FACT_CHARS = 256


class Decision(StrEnum):
    APPROVE = "APPROVE"
    DENY = "DENY"
    ESCALATE = "ESCALATE"


RuleSetName = Literal["refunds", "order_terms", "pricing"]
FactType = Literal["int", "double", "string", "bool"]

# Facts each rule set may reference: name -> (type, description). All are nullable.
FACT_SCHEMAS: dict[str, dict[str, tuple[FactType, str]]] = {
    "refunds": {
        "order_id": ("string", "Merchant order id"),
        "requested_amount_minor": ("int", "Requested refund in minor units (cents)"),
        "currency": ("string", "ISO-4217 code, e.g. USD"),
        "reason": (
            "string",
            "damaged | not_as_described | not_received | late_delivery | wrong_item | "
            "changed_mind | other",
        ),
        "days_since_delivery": ("int", "Days since delivery; null when unknown"),
        "item_condition": ("string", "unopened | opened | used | damaged | missing; or null"),
        "channel": ("string", "merchant_user | shopper_agent"),
        "facts_verified": (
            "bool",
            "true when a merchant user entered the facts; false when an external AI agent "
            "asserted them",
        ),
    },
    "order_terms": {
        "order_total_minor": ("int", "Order total in minor units"),
        "currency": ("string", "ISO-4217 code of the order"),
        "line_count": ("int", "Number of order lines"),
        "unmatched_line_count": ("int", "Lines with no confident catalog SKU match"),
        "min_match_confidence": ("double", "Lowest SKU match confidence across lines, 0..1"),
        "max_price_variance_pct": (
            "double",
            "Largest absolute % difference between a quoted unit price and the catalog price",
        ),
        "customer_known": ("bool", "Buyer is mapped to an ERP customer"),
        "customer_ref": ("string", "Buyer reference from the authenticated channel"),
        "payment_terms_days": ("int", "Payment terms requested on the PO; null when absent"),
        "customer_payment_terms_days": ("int", "Payment terms agreed with this customer"),
        "extraction_confidence": ("double", "Model confidence in the extracted PO, 0..1"),
        "source": ("string", "upload | webhook | gmail"),
        "has_po_number": ("bool", "The PO carries a purchase-order number"),
        "requested_delivery_days": ("int", "Days until the requested delivery date; null"),
    },
    "pricing": {
        "sku": ("string", "Catalog SKU"),
        "quantity": ("int", "Units"),
        "unit_price_minor": ("int", "Offered unit price in minor units"),
        "list_price_minor": ("int", "Catalog list price in minor units"),
        "discount_pct": ("double", "Discount off list price, percent"),
        "customer_ref": ("string", "Buyer reference"),
        "currency": ("string", "ISO-4217 code"),
        "channel": ("string", "merchant_user | shopper_agent | b2b_partner"),
    },
}

# Tunable thresholds of the baseline guards: name -> (default, min, max).
PARAM_SCHEMAS: dict[str, dict[str, tuple[float, float, float]]] = {
    "refunds": {"unverified_escalation_minor": (5000, 0, 10_000_000_00)},
    "order_terms": {
        "min_extraction_confidence": (0.7, 0.0, 1.0),
        "max_price_variance_pct": (5.0, 0.0, 100.0),
    },
    "pricing": {},
}

# CEL functions and operators a rule may use. No comprehensions (exists/all/map),
# no timestamps; every referenced identifier must be a declared fact.
ALLOWED_FUNCTIONS = frozenset(
    {
        "!_", "-_", "_&&_", "_||_", "_?_:_", "@not_strictly_false", "@in", "_[_]",
        "_==_", "_!=_", "_<_", "_<=_", "_>_", "_>=_",
        "_+_", "_-_", "_*_", "_/_", "_%_",
        "size", "contains", "startsWith", "endsWith", "matches",
        "int", "double", "string",
    }
)  # fmt: skip


class RuleValidationError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class FactValidationError(ValueError):
    pass


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    description: str = Field(default="", max_length=300)
    when: str = Field(min_length=1, max_length=MAX_EXPRESSION_CHARS)
    decision: Decision
    reason: str = Field(min_length=1, max_length=200)
    max_refund_minor: int | None = Field(default=None, ge=0, le=10_000_000_00)


class RuleSet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    merchant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    rule_set: RuleSetName
    version: str = Field(default="v0", max_length=64)
    default_decision: Decision = Decision.ESCALATE
    params: dict[str, float] = Field(default_factory=dict)
    rules: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)

    @field_validator("rules")
    @classmethod
    def _unique_ids(cls, rules: list[Rule]) -> list[Rule]:
        ids = [r.id for r in rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        if any(i.startswith("sys_") for i in ids):
            raise ValueError("the sys_ prefix is reserved for baseline rules")
        return rules


def resolved_params(rule_set: str, params: dict[str, float]) -> dict[str, float]:
    schema = PARAM_SCHEMAS[rule_set]
    unknown = set(params) - set(schema)
    if unknown:
        raise RuleValidationError([f"unknown params: {', '.join(sorted(unknown))}"])
    out: dict[str, float] = {}
    for name, (default, low, high) in schema.items():
        value = float(params.get(name, default))
        if not math.isfinite(value) or not low <= value <= high:
            raise RuleValidationError([f"param {name} must be between {low} and {high}"])
        out[name] = value
    return out


def baseline_rules(rule_set: str, params: dict[str, float]) -> list[Rule]:
    """Guards every merchant gets, whatever its compiled policy says."""
    p = resolved_params(rule_set, params)
    if rule_set == "refunds":
        return [
            Rule(
                id="sys_unverified_high_value",
                description="Facts asserted by an external AI agent above the threshold",
                when=(
                    "!facts_verified && requested_amount_minor > "
                    f"{int(p['unverified_escalation_minor'])}"
                ),
                decision=Decision.ESCALATE,
                reason="unverified claim above the auto-decision threshold",
            )
        ]
    if rule_set == "order_terms":
        return [
            Rule(
                id="sys_unmatched_lines",
                when="unmatched_line_count > 0",
                decision=Decision.ESCALATE,
                reason="some lines have no confident catalog match",
            ),
            Rule(
                id="sys_unknown_customer",
                when="!customer_known",
                decision=Decision.ESCALATE,
                reason="buyer is not mapped to an ERP customer",
            ),
            Rule(
                id="sys_low_extraction_confidence",
                when=f"extraction_confidence < {p['min_extraction_confidence']!r}",
                decision=Decision.ESCALATE,
                reason="purchase order could not be read with enough confidence",
            ),
            Rule(
                id="sys_price_variance",
                when=f"max_price_variance_pct > {p['max_price_variance_pct']!r}",
                decision=Decision.ESCALATE,
                reason="quoted prices differ from the catalog",
            ),
        ]
    return []


def compile_expression(rule_set: str, expression: str) -> Any:
    """Parse and statically check one CEL expression. Returns the compiled program."""
    if len(expression) > MAX_EXPRESSION_CHARS:
        raise RuleValidationError([f"expression longer than {MAX_EXPRESSION_CHARS} chars"])
    try:
        program = cel.compile(expression)
    except ValueError as exc:
        first_line = str(exc).splitlines()[0] if str(exc) else "syntax error"
        raise RuleValidationError([first_line[:300]]) from exc
    problems: list[str] = []
    unknown_vars = set(program.variables()) - set(FACT_SCHEMAS[rule_set])
    if unknown_vars:
        problems.append(f"unknown facts: {', '.join(sorted(unknown_vars))}")
    banned = set(program.functions()) - ALLOWED_FUNCTIONS
    if banned:
        problems.append(f"functions not allowed: {', '.join(sorted(banned))}")
    if problems:
        raise RuleValidationError(problems)
    return program


@dataclass(frozen=True)
class CompiledRule:
    rule: Rule
    program: Any
    system: bool


@dataclass(frozen=True)
class CompiledRuleSet:
    rule_set: RuleSet
    rules: tuple[CompiledRule, ...]


def compile_rule_set(rule_set: RuleSet) -> CompiledRuleSet:
    """Validate every rule (user and baseline). Raises RuleValidationError listing all problems."""
    problems: list[str] = []
    compiled: list[CompiledRule] = []
    try:
        guards = baseline_rules(rule_set.rule_set, rule_set.params)
    except RuleValidationError as exc:
        raise RuleValidationError(exc.problems) from exc
    for rule, system in [(g, True) for g in guards] + [(r, False) for r in rule_set.rules]:
        if rule.max_refund_minor is not None and (
            rule.decision != Decision.APPROVE or rule_set.rule_set != "refunds"
        ):
            problems.append(f"{rule.id}: max_refund_minor only applies to APPROVE refund rules")
            continue
        try:
            compiled.append(
                CompiledRule(rule, compile_expression(rule_set.rule_set, rule.when), system)
            )
        except RuleValidationError as exc:
            problems.extend(f"{rule.id}: {p}" for p in exc.problems)
    if problems:
        raise RuleValidationError(problems)
    return CompiledRuleSet(rule_set, tuple(compiled))


def normalize_facts(rule_set: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Type-check facts against the rule set's schema; unknown keys are dropped."""
    schema = FACT_SCHEMAS[rule_set]
    out: dict[str, Any] = {}
    for name, (kind, _) in schema.items():
        value = facts.get(name)
        if value is None:
            out[name] = None
        elif kind == "bool":
            if not isinstance(value, bool):
                raise FactValidationError(f"fact {name} must be a boolean")
            out[name] = value
        elif kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise FactValidationError(f"fact {name} must be an integer")
            if abs(value) >= 2**63:
                raise FactValidationError(f"fact {name} is out of range")
            out[name] = value
        elif kind == "double":
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise FactValidationError(f"fact {name} must be a number")
            if not math.isfinite(float(value)):
                raise FactValidationError(f"fact {name} must be finite")
            out[name] = float(value)
        else:
            if not isinstance(value, str):
                raise FactValidationError(f"fact {name} must be a string")
            out[name] = value[:MAX_STRING_FACT_CHARS]
    return out


@dataclass
class Evaluation:
    decision: Decision
    reasons: list[str]
    matched_rule_ids: list[str]
    max_refund_minor: int | None = None
    errored_rule_ids: list[str] = field(default_factory=list)


def evaluate(compiled: CompiledRuleSet, facts: dict[str, Any]) -> Evaluation:
    """Deterministic evaluation; `facts` must come from `normalize_facts`."""
    matched: list[Rule] = []
    errored: list[Rule] = []
    for item in compiled.rules:
        try:
            result = item.program.execute(facts)
        except Exception:  # CEL raises TypeError/RuntimeError/ValueError on bad operands
            errored.append(item.rule)
            continue
        if result is True:
            matched.append(item.rule)
        elif result is not False:
            errored.append(item.rule)

    def of(decision: Decision) -> list[Rule]:
        return [r for r in matched if r.decision == decision]

    failed_guards = [r for r in errored if r.decision != Decision.APPROVE]
    matched_ids = [r.id for r in matched]
    errored_ids = [r.id for r in errored]
    if denies := of(Decision.DENY):
        return Evaluation(Decision.DENY, [r.reason for r in denies], matched_ids, None, errored_ids)
    escalations = of(Decision.ESCALATE)
    if escalations or failed_guards:
        reasons = [r.reason for r in escalations] + [
            f"rule {r.id} could not be evaluated with the given facts" for r in failed_guards
        ]
        return Evaluation(Decision.ESCALATE, reasons, matched_ids, None, errored_ids)
    if approvals := of(Decision.APPROVE):
        caps = [r.max_refund_minor for r in approvals if r.max_refund_minor is not None]
        return Evaluation(
            Decision.APPROVE,
            [r.reason for r in approvals],
            matched_ids,
            min(caps) if caps else None,
            errored_ids,
        )
    default = compiled.rule_set.default_decision
    return Evaluation(default, [f"no rule matched; default {default.value}"], [], None, errored_ids)


# --------------------------------------------------------------------------- LLM output schema
def _fact_json_type(kind: FactType) -> dict[str, Any]:
    base = {"int": "integer", "double": "number", "string": "string", "bool": "boolean"}[kind]
    return {"anyOf": [{"type": base}, {"type": "null"}]}


def draft_json_schema(rule_set: str) -> dict[str, Any]:
    """JSON Schema that Gemini's compiled policy must follow (structured output)."""
    decision = {"type": "string", "enum": [d.value for d in Decision]}
    facts_schema = {
        "type": "object",
        "properties": {
            name: {**_fact_json_type(kind), "description": desc}
            for name, (kind, desc) in FACT_SCHEMAS[rule_set].items()
        },
    }
    rule = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "snake_case, starts with a letter, <= 40"},
            "description": {"type": "string"},
            "when": {"type": "string", "description": "CEL boolean expression over the facts"},
            "decision": decision,
            "reason": {"type": "string", "description": "Short reason shown to reviewers"},
            "max_refund_minor": {"anyOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
        },
        "required": ["id", "description", "when", "decision", "reason"],
    }
    test_case = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "facts": facts_schema,
            "expected_decision": decision,
        },
        "required": ["name", "facts", "expected_decision"],
    }
    return {
        "type": "object",
        "properties": {
            "rules": {"type": "array", "items": rule, "maxItems": 50},
            "default_decision": decision,
            "test_cases": {"type": "array", "items": test_case, "maxItems": 12},
            "assumptions": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
        },
        "required": ["rules", "default_decision", "test_cases"],
    }

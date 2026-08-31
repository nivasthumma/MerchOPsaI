"""Typed tool + evidence contracts — CONTRACT §13, §14 (as amended by ADR-0008)."""
from __future__ import annotations

import enum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class RiskClass(str, enum.Enum):
    """The risk a tool carries by construction — MerchantOps §24.

    This is a FLOOR, not a verdict. `app.policy.risk` may raise a call above the
    declared class based on what it is actually being asked to do; it may never
    lower one below it.
    """
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class Evidence(BaseModel):
    """A single piece of evidence returned by a tool.

    `untrusted` is the injection-defence tag required by CONTRACT §36. Any
    value that originated as merchant/customer free text MUST carry it, and
    the prompt renderer wraps such values in explicit delimiters.
    """
    key: str
    value: Any
    source: str                                  # table/tool the value came from
    untrusted: bool = False

    @field_validator("value")
    @classmethod
    def _jsonable(cls, v: Any) -> Any:
        if isinstance(v, (str, int, float, bool, type(None), list, dict)):
            return v
        return str(v)


class ToolResult(BaseModel):
    """CONTRACT §14 normalised tool result."""
    success: bool
    data: dict = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    external_reference: str | None = None
    error_code: str | None = None

    # populated for sensitive operations (CONTRACT §14)
    risk_level: str | None = None
    policy_decision: str | None = None
    approval_required: bool | None = None
    approval_id: str | None = None

    def redacted(self) -> dict:
        d = self.model_dump()
        return d


FindingKind = Literal["OBSERVED", "INFERRED", "RECOMMENDED"]


class Finding(BaseModel):
    """CONTRACT §14 (amended). Makes §17's Observed/Inferred/Recommended split
    a schema constraint, and makes §29's 'evidence grounding' computable
    without an LLM judge.
    """
    claim: str
    kind: FindingKind
    evidence_refs: list[str] = Field(default_factory=list)   # tool_call ids
    metric: str | None = None
    value: Any = None

    @field_validator("evidence_refs")
    @classmethod
    def _strip(cls, v: list[str]) -> list[str]:
        return [x for x in v if isinstance(x, str) and x.strip()]

    def is_grounded(self, valid_tool_call_ids: set[str]) -> bool:
        """OBSERVED claims require at least one resolvable tool_call id.

        INFERRED/RECOMMENDED are conclusions drawn from observations; they are
        not required to cite directly, but an OBSERVED claim with no resolvable
        citation is an ungrounded claim (CONTRACT §14 grounding_rate).
        """
        if self.kind != "OBSERVED":
            return True
        return any(ref in valid_tool_call_ids for ref in self.evidence_refs)


class ToolSpec(BaseModel):
    """CONTRACT §13 — every tool declares all of this."""
    name: str
    description: str
    input_schema: dict
    required_permissions: list[str]
    risk_class: RiskClass
    timeout_seconds: float = 10.0
    max_retries: int = 0
    idempotent: bool = True
    audit_required: bool = False
    data_scope: str = "merchant"      # merchant | global

    # MerchantOps §24 lists reversibility as a risk input. Declared here rather
    # than inferred, because whether an effect can be undone is a property of
    # the operation, not something to guess from its arguments. A refund moves
    # money out; a notification cannot be unsent.
    reversible: bool = True

    def to_anthropic_tool(self) -> dict:
        """Anthropic tool definition. strict=True + additionalProperties:false
        so tool inputs validate exactly (CONTRACT §13 argument validation)."""
        schema = dict(self.input_schema)
        schema.setdefault("additionalProperties", False)
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": schema,
        }

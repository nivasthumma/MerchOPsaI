"""Scenario contract — CONTRACT §32."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class Expect(BaseModel):
    # Tool behaviour: either an exact sequence or an acceptable set.
    tool_sequence: list[str] | None = None
    tools_include: list[str] = Field(default_factory=list)
    tools_forbidden: list[str] = Field(default_factory=list)

    policy_decision: str | None = None          # decision for the graded tool
    policy_rule: str | None = None
    approval_required: bool | None = None
    final_status: str | None = None
    verification_state: str | None = None
    failure_code: str | None = None

    # Safety assertions
    external_calls: int | None = None
    no_financial_effect: bool = False
    answer_contains: list[str] = Field(default_factory=list)
    answer_excludes: list[str] = Field(default_factory=list)
    min_grounding_rate: float | None = None


class Scenario(BaseModel):
    id: str
    description: str
    category: Literal[
        "revenue_investigation", "payment_failure", "duplicate_payment",
        "refund_policy", "adversarial_security", "failure_unknown",
    ]
    critical: bool = False                       # CONTRACT §53 stop condition
    principal: str = "owner"                     # owner | analyst | owner_b
    request: str
    initial_state: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] | None = None
    approve: bool | None = None                  # simulate the human decision
    fault: dict | None = None                    # CONTRACT §35A injection
    reverify: bool = False
    expect: Expect = Field(default_factory=Expect)


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""

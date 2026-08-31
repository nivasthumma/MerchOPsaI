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
    refund_delta: int | None = None              # exact change in refund rows
    action_status: str | None = None
    approval_decision: str | None = None
    audit_events: list[str] = Field(default_factory=list)
    audit_excludes_secrets: bool = False
    answer_contains: list[str] = Field(default_factory=list)
    answer_excludes: list[str] = Field(default_factory=list)
    min_grounding_rate: float | None = None

    # --- detection assertions (MerchantOps §12, §13, §60) ---
    incidents_created: int | None = None
    incident_types: list[str] = Field(default_factory=list)        # must be present
    incident_types_absent: list[str] = Field(default_factory=list)
    incident_severity: str | None = None            # severity of the top incident
    min_revenue_at_risk_minor: int | None = None
    incident_signals_include: list[str] = Field(default_factory=list)
    degraded_methods: list[str] | None = None       # exact set that tripped the rule
    second_sweep_creates: int | None = None         # idempotency
    incident_status_after: str | None = None
    incident_trace_events: list[str] = Field(default_factory=list)
    max_detection_ms: int | None = None             # §60: detection < 60s
    foreign_incidents: int | None = None            # cross-merchant leakage
    onset_hour_utc_between: list[int] | None = None  # [lo, hi] — §51 timeline

    # --- webhook assertions (MerchantOps §34, §35) ---
    webhook_status: str | None = None            # status of the LAST delivery
    webhook_events_stored: int | None = None     # rows in the durable store
    webhook_actions_reverified: int | None = None
    webhook_raises_incident: bool | None = None


class Scenario(BaseModel):
    id: str
    description: str
    category: Literal[
        "revenue_investigation", "payment_failure", "duplicate_payment",
        "refund_policy", "adversarial_security", "failure_unknown",
        # MerchantOps §12/§13. Detection scenarios have no request and no task;
        # they grade the sweep and the incident it produced.
        "detection",
        # MerchantOps §34/§35. Delivery, deduplication, signature, and what a
        # provider event is and is not allowed to decide.
        "webhook",
    ]
    critical: bool = False                       # CONTRACT §53 stop condition
    principal: str = "owner"                     # owner | analyst | owner_b
    request: str = ""
    initial_state: dict[str, Any] = Field(default_factory=dict)

    # --- detection scenarios (MerchantOps §12, §13) ---
    detect_for: list[str] = Field(default_factory=list)   # merchants to sweep, in order
    detect_twice: bool = False                            # assert idempotency
    investigate_first: bool = False                       # dispatch the agent at the top incident
    allowed_tools: list[str] | None = None
    approve: bool | None = None                  # simulate the human decision
    approve_as: str | None = None                # approve as a DIFFERENT principal
    expire_approval: bool = False                # back-date the approval past its TTL
    budget: dict | None = None                   # override the execution budget
    fault: dict | None = None                    # CONTRACT §35A injection
    reverify: bool = False
    reconcile: bool = False                      # run the reconciliation sweep
    # MerchantOps §34. Delivered after the action exists, so the scenario grades
    # what an event does to a real action rather than to an empty database.
    #   {event, sign, event_id, deliver_times, break_provider_state}
    webhook: dict | None = None
    repeat_request: bool = False                 # run the same request again as a 2nd task
    expect: Expect = Field(default_factory=Expect)


class CheckResult(BaseModel):
    name: str
    passed: bool
    detail: str = ""

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

    # --- multivariate correlation (MerchantOps v2 §18) ---
    # How many independent RULES saw the episode a named incident belongs to,
    # itself included. 1 means the signal stands alone.
    incident_corroboration: dict[str, int] = Field(default_factory=dict)
    # {incident_type: [rule, ...]} — which other rules corroborated it. Exact
    # set, so a rule appearing that should not is a failure and not a shrug.
    corroborating_rules: dict[str, list[str]] = Field(default_factory=dict)
    # Every incident's correlation facts are internally consistent, and no rule
    # is ever recorded as corroborating itself.
    correlation_is_coherent: bool | None = None

    # --- computed confidence (MerchantOps v2 §33) ---
    # The band the PLATFORM assigned to a named incident after investigation.
    confidence_band: dict[str, str] = Field(default_factory=dict)
    # The asymmetry: recomputing without the model's number must never yield a
    # WEAKER band than the one stored. If it does, the model raised it.
    model_confidence_cannot_raise: bool | None = None
    # Untrusted evidence must not appear among the corroborating sources.
    untrusted_evidence_excluded: bool | None = None
    # Incidents carrying no band at all. An unassessed incident has no
    # confidence, and a default would assert a view nobody formed — so this
    # asserts the null rather than letting an absent band pass by being absent.
    unassessed_incidents: int | None = None

    # --- webhook assertions (MerchantOps §34, §35) ---
    webhook_status: str | None = None            # status of the LAST delivery
    webhook_events_stored: int | None = None     # rows in the durable store
    webhook_actions_reverified: int | None = None
    webhook_raises_incident: bool | None = None

    # --- risk / approval assertions (MerchantOps §24, §25, §26) ---
    risk_level: str | None = None                # graded risk of the halted action
    risk_was_raised: bool | None = None          # above the tool's declared floor
    required_signatures: int | None = None
    signatures_collected: int | None = None
    risk_factors_include: list[str] = Field(default_factory=list)

    # --- agent output assertions (MerchantOps §36, §37) ---
    agent_intent: str | None = None
    has_recommendation: bool | None = None
    has_model_findings: bool | None = None
    model_findings_grounded: bool | None = None
    confidence_between: list[float] | None = None      # [lo, hi]
    answer_excludes_output_block: bool = False

    # --- recovery assertions (MerchantOps §22, §23, §27, §28) ---
    plan_intervention: str | None = None
    plan_candidates: int | None = None
    plan_eligible_candidates: int | None = None
    plan_executable_candidates: int | None = None
    plan_status: str | None = None
    recovery_ordering_holds: bool | None = None    # §49: at risk >= eligible >= expected
    ineligible_reasons_include: list[str] = Field(default_factory=list)
    stop_rule: str | None = None
    dispatch_refused: bool | None = None
    plan_is_idempotent: bool | None = None
    no_financial_effect_from_planning: bool = False

    # --- ledger assertions (MerchantOps §49) ---
    ledger_invariants_hold: bool | None = None
    ledger_recovered_minor: int | None = None
    ledger_attempted_gt_zero: bool | None = None
    ledger_unknown_gt_zero: bool | None = None
    candidate_status_after: str | None = None

    # --- observability assertions (MerchantOps §41, §47, §56, §57, §58) ---
    failure_category: str | None = None
    failure_retryability: str | None = None
    failure_owner: str | None = None
    records_all_versions: bool | None = None
    one_correlation_id: bool | None = None
    canonical_events_include: list[str] = Field(default_factory=list)

    # --- transcript assertions (MerchantOps §38, §66) ---
    transcript_recorded: bool | None = None
    transcript_has_final_answer: bool | None = None
    transcript_flags_untrusted: bool | None = None
    transcript_excludes: list[str] = Field(default_factory=list)


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
        # MerchantOps §24/§25/§26. Computed risk, the floor rule, and the
        # second pair of eyes.
        "risk_approval",
        # MerchantOps §22/§23/§27/§28. What could be done, what it is worth,
        # what bounds it, and when to stop.
        "recovery",
    ]
    critical: bool = False                       # CONTRACT §53 stop condition
    principal: str = "owner"                     # owner | analyst | owner_b
    request: str = ""
    initial_state: dict[str, Any] = Field(default_factory=dict)

    # --- detection scenarios (MerchantOps §12, §13) ---
    detect_for: list[str] = Field(default_factory=list)   # merchants to sweep, in order
    detect_twice: bool = False                            # assert idempotency
    investigate_first: bool = False                       # dispatch the agent at the top incident

    # --- recovery scenarios (MerchantOps §23, §27, §28) ---
    plan_for: str | None = None            # incident type to plan recovery for
    budget_override: dict = Field(default_factory=dict)   # shrink a bound to make it bite
    dispatch_top_candidate: bool = False
    # Shrink a campaign to one action so it is not bulk and can be dispatched.
    single_candidate: bool = False
    approve_dispatched: bool = False
    settle_after_dispatch: bool = False
    allowed_tools: list[str] | None = None
    approve: bool | None = None                  # simulate the human decision
    approve_as: str | None = None                # approve as a DIFFERENT principal
    # MerchantOps §26. Principals who sign, in order, after the first approval.
    # "owner" twice is the self-approval case and must be refused.
    co_approvers: list[str] = Field(default_factory=list)
    # Plant an unsettled action on this payment before the run, so the risk
    # engine's uncertainty factor has something to find.
    unsettled_action_on: str | None = None
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

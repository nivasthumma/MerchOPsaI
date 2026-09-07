"""Response contracts — ADR-0032.

Every endpoint returned a bare `dict`. FastAPI therefore had nothing to put in
the OpenAPI document, `/docs` listed routes with no shapes, and
`web/src/api/types.ts` was 411 hand-written lines mirroring dictionary literals
in `app/api/main.py` — a mirror nothing compared to the thing it mirrored. A
field renamed on one side and not the other broke at runtime, in a browser,
with a green build behind it.

## `extra="forbid"` is the load-bearing decision

A `response_model` **filters**: a key the model does not declare is dropped from
the response, silently. Adopting response models naively would therefore be a
way to *cause* the exact bug this is meant to prevent — model a response, miss a
field, and the frontend stops receiving it with nothing failing anywhere.

So every contract forbids extra keys. A dict carrying a field the model does not
declare is now a `ResponseValidationError` rather than a quiet truncation, which
turns the existing test suite into the verifier: any field left unmodelled fails
a test that already exists.

## `response_model_exclude_unset=True` is the other half

`approve()` adds `awaiting_signatures` only when signatures are outstanding, and
several endpoints build a view conditionally. Without `exclude_unset` those
fields would appear as `null` on every response, changing payloads the frontend
already reads. With it, a field absent from the returned dict stays absent — so
the schema gains precision without any response changing shape.

## Where a shape is genuinely open

Audit payloads, tool arguments and tool output are JSON whose shape belongs to
the event, not to this module. Those are typed `dict` rather than modelled, and
the ones keyed by data (`{status: count}`) are `dict[str, int]`. Inventing a
rigid schema for them would be precision this API does not actually have.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.failures import Retryability
from app.models import TaskStatus, VerificationState

# Derived from the enums rather than restated. A hand-copied union is a second
# place for the same fact to live, and the frontend narrows on these — a value
# outside the set is a runtime surprise in a `switch` somebody wrote.
TaskStatusLiteral = Literal[tuple(s.value for s in TaskStatus)]                # type: ignore[valid-type]
VerificationStateLiteral = Literal[tuple(s.value for s in VerificationState)]  # type: ignore[valid-type]
RetryabilityLiteral = Literal[tuple(r.value for r in Retryability)]            # type: ignore[valid-type]
MessageRoleLiteral = Literal["user", "assistant"]


# A measurement that may be a whole number or a fraction depending on what
# produced it. `float` alone would be wrong in a subtle way: Pydantic coerces,
# so a p50 of 43 would be served as 43.0 — a response changing shape because of
# the contract that was supposed to describe it. The union keeps whatever the
# producer emitted.
Number = int | float


class Contract(BaseModel):
    """Base for every response model. See the module docstring for why this
    forbids extra keys rather than ignoring them."""
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- primitives
class FailureClassView(Contract):
    """MerchantOps §56. A code says what broke; this says whether trying again
    is even the question."""
    error_code: str
    category: str
    retryability: RetryabilityLiteral
    owning_subsystem: str
    recommended_next_action: str
    correlation_id: str | None
    evidence: list
    is_classified: bool


class RunVersions(Contract):
    """MerchantOps §41 — everything needed to reproduce a run."""
    agent: str
    model_provider: str | None
    model: str | None
    prompt: str | None
    tool_registry: str | None
    policy: str | None
    workflow: str | None


class FindingView(Contract):
    """§20's FACT / INFERENCE / RECOMMENDATION split, as stored.

    Findings come from two places and stay distinguishable. A deterministic
    OBSERVED finding is built from what a tool returned and carries `metric` and
    `value`; a model finding is tagged `source: "model"` and carries its own
    `finding_type` and the `E`-labels it cited. Both shapes are served on the
    same list, so both sets of fields are optional here — which is the schema
    telling the truth about a union rather than pretending it is one shape.
    """
    claim: str
    kind: str
    evidence_refs: list[str] = []
    metric: str | None = None
    value: object | None = None
    # Model findings only.
    source: str | None = None
    finding_type: str | None = None
    evidence_ids: list[str] | None = None


class RecommendationView(Contract):
    type: str
    # Always sent (the view builds both keys together), so required-and-nullable
    # rather than optional. An optional field tells a client it may be absent,
    # which forces a check nobody needs and is simply not true here.
    detail: str | None


class ApprovalView(Contract):
    id: str
    decision: str
    action_type: str
    action_payload: dict
    risk_level: str
    expires_at: str
    decided_by: str | None
    required_signatures: int
    signed_by: list[str]


class VerificationDetail(Contract):
    """The verdict, the sentence explaining it, and what it was computed from.

    A JSON column rather than a string, and modelled rather than left as `dict`
    because the frontend narrows on `state` — leaving it open meant the
    generated type was `{[key: string]: unknown}`, which the hand-written mirror
    was quietly asserting more than.
    """
    state: VerificationStateLiteral
    reason: str
    expected: dict | None = None
    actual: dict | None = None
    external_reference: str | None = None


class ActionView(Contract):
    """The action as it appears inside a task."""
    id: str
    action_type: str
    status: str
    target_payment_id: str | None
    external_payment_id: str | None
    amount_minor: int | None
    external_reference: str | None
    verification_state: VerificationStateLiteral | None
    verification_detail: VerificationDetail | None
    verify_attempts: int


class TaskView(Contract):
    id: str
    tenant_id: str | None
    merchant_id: str
    user_id: str
    request: str
    status: TaskStatusLiteral
    final_answer: str | None
    failure_code: str | None
    findings: list[FindingView] | None
    tool_calls: int | None
    intent: str | None
    recommendation: RecommendationView | None
    agent_confidence: Number | None
    requires_human: bool
    model_requires_human: bool
    llm_turns: int | None
    duration_ms: int | None
    versions: RunVersions
    agent_version: str
    model_version: str | None
    prompt_version: str | None
    failure: FailureClassView | None
    is_replay: bool
    replayed_from: str | None
    approvals: list[ApprovalView]
    actions: list[ActionView]
    # Plan P0-08. Derived from recorded rows, never from model prose.
    activity: list[ActivityStep] = []
    # Present only when a CRITICAL action is waiting on a second signature.
    # `exclude_unset` keeps them absent otherwise rather than null.
    awaiting_signatures: int | None = None
    signed_by: list[str] | None = None


# -------------------------------------------------------------------- traces
class TraceEvent(Contract):
    id: int
    at: str
    event: str
    canonical_event: str | None = None
    correlation_id: str | None = None
    task_id: str | None = None
    incident_id: str | None = None
    # The payload's shape belongs to the event, not to this module.
    payload: dict


class TaskTrace(Contract):
    task_id: str
    trace: list[TraceEvent]


class CorrelationTrace(Contract):
    """§58's complete trace: everything one operation touched, in one ordering."""
    correlation_id: str
    events: list[TraceEvent]
    span_count: int


# ------------------------------------------------------------------ evidence
class ToolCallView(Contract):
    id: str
    seq: int
    tool: str
    arguments: dict | None = None
    success: bool
    error_code: str | None = None
    risk_level: str | None = None
    policy_decision: str | None = None
    duration_ms: int | None = None
    evidence: list = []
    data: dict = {}


class TaskEvidence(Contract):
    task_id: str
    tool_calls: list[ToolCallView]


class MessageView(Contract):
    seq: int
    turn: int
    role: MessageRoleLiteral
    content: list
    contains_untrusted: bool
    char_count: int
    at: str


class TaskMessages(Contract):
    task_id: str
    messages: list[MessageView]
    total_chars: int


# ----------------------------------------------------------------- approvals
class ApprovalQueueItem(Contract):
    id: str
    task_id: str
    action_type: str
    action_payload: dict
    risk_level: str
    decision: str
    decided_by: str | None = None
    required_signatures: int
    signed_by: list[str] = []
    created_at: str
    expires_at: str
    expired: bool


class ApprovalQueue(Contract):
    approvals: list[ApprovalQueueItem]


# ------------------------------------------------------------------- actions
class EscalatedAction(Contract):
    """One row of the reconciliation work queue — plan P0-04.

    Every field the plan names is here, because a queue that lists identifiers
    is not a work queue: age (`created_at`), amount, provider, external
    reference, last known state, attempts, last check, next retry, escalation,
    owner, and the incident and task it came from.

    Timestamps are `object` rather than `str` on the fields that come straight
    out of a `text()` query: those arrive as `datetime` and are serialised by
    FastAPI. Declaring them `str` would coerce and change the wire format of a
    response the frontend already parses.
    """
    id: str
    task_id: str
    merchant_id: str
    action_type: str | None = None
    status: str | None = None
    target_payment_id: str | None = None
    external_payment_id: str | None = None
    amount_minor: int | None = None
    external_reference: str | None = None
    verification_state: str | None = None
    verify_attempts: int
    created_at: str | object = None
    updated_at: str | object = None
    verification_detail: dict | None = None
    # --- P0-04 reconciliation workflow ---
    escalated: bool = False
    escalated_at: str | object = None
    last_verified_at: str | object = None
    next_verify_at: str | object = None
    provider: str | None = None
    environment: str | None = None
    incident_id: str | None = None
    owner: str | None = None


# --- Action Center (P0-03) -------------------------------------------------
class ActionRow(Contract):
    """One action in the Action Center, in every section that lists actions.

    Deliberately one shape for all four action sections. A per-section model is
    how "amount_minor" comes to mean the requested amount in one column and the
    verified amount in another.
    """
    id: str
    task_id: str
    merchant_id: str
    action_type: str
    status: str
    target_payment_id: str | None = None
    external_payment_id: str | None = None
    external_reference: str | None = None
    amount_minor: int | None = None
    verification_state: str | None = None
    verify_attempts: int
    escalated: bool = False
    escalated_at: str | object = None
    last_verified_at: str | object = None
    next_verify_at: str | object = None
    approval_id: str | None = None
    recovery_candidate_id: str | None = None
    created_at: str | object = None
    updated_at: str | object = None
    provider_latency_ms: Number | None = None
    verification_latency_ms: Number | None = None
    customer_id: str | None = None
    payment_method: str | None = None
    provider: str | None = None
    environment: str | None = None
    incident_id: str | None = None
    owner: str | None = None
    task_request: str | None = None
    task_status: str | None = None
    approval_decision: str | None = None
    risk_level: str | None = None
    expires_at: str | object = None
    required_signatures: int | None = None


class PendingApprovalRow(Contract):
    """An approval no action exists for yet — the money has not moved.

    Separate from `ActionRow` because there is genuinely no action row to
    describe: the claim is not made until the approval clears. Modelling it as
    an action with null everything would tell an operator an action exists.
    """
    approval_id: str
    task_id: str
    action_type: str
    action_payload: dict
    risk_level: str
    decision: str
    expires_at: str | object = None
    required_signatures: int
    created_at: str | object = None
    evidence: list = []
    incident_id: str | None = None
    owner: str | None = None
    task_request: str | None = None
    signatures: int
    expired: bool


class ActionCenterCounts(Contract):
    awaiting_approval: int
    executing: int
    unknown: int
    escalated: int
    recently_completed: int


class ReconciliationPolicy(Contract):
    max_attempts: int
    on_exhaustion: str


class ActionCenter(Contract):
    generated_at: str
    merchant_id: str
    awaiting_approval: list[PendingApprovalRow]
    executing: list[ActionRow]
    unknown: list[ActionRow]
    escalated: list[ActionRow]
    recently_completed: list[ActionRow]
    # TRUE totals, counted in SQL — not the length of the page. The two
    # disagreed once, and the smaller number was on the screen an operator
    # acts from.
    counts: ActionCenterCounts
    # How many rows each section actually returned, so a client can say
    # "50 of 60" rather than presenting a page length as a total.
    shown: ActionCenterCounts
    limit: int
    reconciliation_policy: ReconciliationPolicy
    sections: list[str]


# --- Command Center (P0-05) ------------------------------------------------
class RevenueHealth(Contract):
    at_risk_minor: int
    recoverable_minor: int
    attempted_minor: int
    recovered_minor: int
    failed_minor: int
    unknown_minor: int
    outstanding_minor: int
    invariants_broken: list[str]


class FunnelStage(Contract):
    """One stage of the recovery funnel — P1-03.

    Ordered and named server-side so at-risk can never be rendered as
    recovered by a client that arranged six loose numbers itself.
    """
    stage: str
    label: str
    amount_minor: int


class AttentionCounts(Contract):
    approvals_pending: int
    approvals_expired: int
    unknown_actions: int
    escalated_actions: int
    open_incidents: int
    critical_incidents: int
    running_tasks: int


class ActivityEvent(Contract):
    event_type: str
    correlation_id: str | None = None
    task_id: str | None = None
    incident_id: str | None = None
    created_at: str | object = None
    payload: dict


class CommandCenter(Contract):
    generated_at: str
    merchant_id: str
    revenue: RevenueHealth
    funnel: list[FunnelStage]
    attention: AttentionCounts
    by_incident: list[IncidentExposure]
    by_method: list[MethodExposure]
    activity: list[ActivityEvent]


# --- Global search (P1-06) -------------------------------------------------
class LifecycleEvent(Contract):
    """One link in §7's chain. Every entry is a row that exists, with its own
    timestamp — never an inferred step."""
    stage: str
    at: str | None = None
    id: str
    label: str
    detail: str = ""
    correlation_id: str | None = None


class LifecyclePayment(Contract):
    id: str
    merchant_id: str
    order_id: str | None = None
    customer_id: str | None = None
    customer_name: str | None = None
    amount_minor: int
    currency: str
    method: str
    status: str
    error_reason: str | None = None
    amount_refunded_minor: int
    refund_status: str | None = None
    created_at: str | None = None


class PaymentLifecycle(Contract):
    """MerchantOps §7 — a payment traceable through its complete lifecycle."""
    payment: LifecyclePayment
    external_payment_id: str | None = None
    provider: str | None = None
    environment: str | None = None
    events: list[LifecycleEvent]
    stages: list[str]
    # More than one, which is the whole reason this endpoint exists.
    correlation_ids: list[str] = []
    incident_ids: list[str] = []
    task_ids: list[str] = []
    action_ids: list[str] = []
    generated_at: str


class SearchHit(Contract):
    kind: str
    id: str
    label: str | None = None
    detail: str | None = None
    created_at: str | None = None
    route: str


class SearchResults(Contract):
    query: str
    results: list[SearchHit]
    truncated: bool


# --- Liveness / readiness (§11) --------------------------------------------
class Liveness(Contract):
    status: str
    checked_at: str


class ComponentHealth(Contract):
    """One dependency's verdict.

    `extra="forbid"` is relaxed here alone: each check attaches the facts that
    make its own verdict actionable — mapping coverage, webhook counts, the
    reconciliation backlog — and a fixed union of every check's extras would be
    a model that has to be edited every time a check learns something new.
    """
    model_config = ConfigDict(extra="allow")

    status: str
    detail: str
    required: bool
    latency_ms: Number


class Readiness(Contract):
    status: str
    checked_at: str
    components: dict[str, ComponentHealth]
    blocking: list[str]
    degraded: list[str]


class ActivityStep(Contract):
    """One step of the agent's operational progress — plan P0-08.

    `state` is the claim: `done` happened and worked, `failed` happened and did
    not, `blocked` is waiting on a person, `running` is in flight, `pending` was
    expected and not reached. A UI narrows on these.
    """
    key: str
    label: str
    state: Literal["done", "failed", "blocked", "running", "pending"]
    at: str | None = None
    detail: str = ""


class ActionDetail(Contract):
    id: str
    task_id: str
    action_type: str
    target_payment_id: str | None = None
    external_payment_id: str | None = None
    amount_minor: int | None = None
    status: str
    verification_state: VerificationStateLiteral | None = None
    verification_detail: VerificationDetail | None = None
    verify_attempts: int
    external_reference: str | None = None
    approval_id: str | None = None
    recovery_candidate_id: str | None = None
    idempotency_key_prefix: str
    provider_latency_ms: Number | None = None
    verification_latency_ms: Number | None = None
    created_at: str
    updated_at: str | None = None


class ReverifyResult(Contract):
    task: TaskView
    verification: dict


class ReconcileReport(Contract):
    scanned: int
    settled: int
    still_unsettled: int
    escalated: int
    skipped_too_recent: int
    details: list[dict]


# ------------------------------------------------------------------ recovery
class PlanBudget(Contract):
    max_recovery_minor: int
    max_actions: int
    max_attempts_per_customer: int
    max_duration_seconds: int


class CandidateView(Contract):
    id: str
    rank: int
    payment_id: str
    customer_id: str | None = None
    amount_minor: int
    intervention: str
    status: str
    ineligible_reason: str | None = None
    expected_recovery_minor: int | None = None
    actual_recovery_minor: int | None = None
    executable: bool
    attempts: int
    task_id: str | None = None


class PlanView(Contract):
    id: str
    incident_id: str
    merchant_id: str
    status: str
    intervention: str
    revenue_at_risk_minor: int
    eligible_recovery_minor: int
    expected_recovery_minor: int
    # A sentence, not a structure — `str` because that is what is served.
    expected_recovery_basis: str | None = None
    budget: PlanBudget
    stop_rule: str | None = None
    stop_reason: str | None = None
    planner_version: str | None = None
    expires_at: str
    candidates: list[CandidateView] | None = None
    # Only on the create route: whether this call planned it or found it.
    created: bool | None = None


# ------------------------------------------------------------------ webhooks
class WebhookEventView(Contract):
    id: str
    event_id: str
    event_type: str
    status: str
    signature_valid: bool
    entity_id: str | None = None
    correlation_id: str | None = None
    occurred_at: str | None = None
    received_at: str
    processed_at: str | None = None
    note: str | None = None


class WebhookEventList(Contract):
    events: list[WebhookEventView]
    # Deliveries that could not be attributed to a merchant are visible in
    # aggregate only; showing their bodies would make an unauthenticated
    # endpoint into a cross-tenant read.
    unattributed_count: int


# ----------------------------------------------------------------- incidents
class IncidentSummary(Contract):
    id: str
    merchant_id: str
    type: str
    severity: str
    status: str
    title: str
    summary: str | None = None
    revenue_at_risk_minor: int
    detection_rule: str | None = None
    detection_version: str | None = None
    correlation_id: str | None = None
    started_at: str
    detected_at: str
    resolved_at: str | None = None
    # Detail only.
    signals: dict | None = None
    evidence: list[dict] | None = None
    recovery: PlanView | None = None
    timeline: list[dict] | None = None
    tasks: list[dict] | None = None
    # The financial actions this incident produced — plan P0-07's last four
    # stages. Same row shape the Action Center serves, so the incident page and
    # the queue cannot disagree about the state of an action.
    actions: list[ActionRow] | None = None
    legal_transitions: list[str] | None = None


class SavedView(Contract):
    """One saved view — plan P1-05. Declared server-side so that "My attention"
    cannot mean one thing in a pasted link and another in the sidebar."""
    key: str
    label: str
    hint: str
    filter: dict
    count: int


class IncidentList(Contract):
    incidents: list[IncidentSummary]
    # Summed over the WHOLE match in SQL, never across the returned page.
    total_revenue_at_risk_minor: int
    # How many matched, and how many are in `incidents`. A client showing the
    # total beside a shorter list is showing a number it cannot substantiate.
    matched: int = 0
    shown: int = 0
    # Plan P1-05. Served with the list so five view counts come from one read
    # at one instant rather than five requests at five.
    views: list[SavedView] = []
    applied_view: str | None = None


class IncidentTrace(Contract):
    incident_id: str
    trace: list[TraceEvent]


# -------------------------------------------------------------------- system
class AgentBudget(Contract):
    configured_wall_clock_seconds: int
    platform_timeout_seconds: int | None = None
    enforced_wall_clock_seconds: int
    capped_by_platform: bool
    max_tool_calls: int
    max_llm_turns: int


class Health(Contract):
    status: str
    llm_provider: str
    llm_credential_source: str | None = None
    llm_provider_is_explicit: bool
    llm_provider_source: str
    llm_model: str
    payment_adapter: str
    razorpay_execution_is_real: bool
    auth: str
    auth_secret_is_development_default: bool
    webhook_signature_verification: bool
    agent_budget: AgentBudget


class Me(Contract):
    tenant_id: str | None = None
    user_id: str
    merchant_id: str
    role: str
    permissions: list[str]


class ProviderChange(Contract):
    llm_provider: str
    llm_provider_source: str
    llm_model: str
    changed_from: str


# ------------------------------------------------------------- detection
class IncidentBrief(Contract):
    """What a detection sweep reports about what it raised."""
    id: str
    type: str
    severity: str
    title: str
    revenue_at_risk_minor: int
    started_at: str


class DetectResult(Contract):
    merchant_id: str
    anomalies_found: int
    incidents_created: int
    already_known: int
    scanned_rules: int
    duration_ms: int
    incidents: list[IncidentBrief]


class InvestigateResult(Contract):
    incident: IncidentSummary
    task: TaskView


# -------------------------------------------------------------- taxonomy
class FailureTaxonomyEntry(Contract):
    error_code: str
    category: str
    retryability: str
    owning_subsystem: str
    recommended_next_action: str


class FailureTaxonomy(Contract):
    failures: list[FailureTaxonomyEntry]


# --------------------------------------------------------------- metrics
class MetricsStrip(Contract):
    """Business counts for the operations strip, scoped to one merchant."""
    window_hours: int
    gated: int
    approved: int
    rejected: int
    moved_minor: int
    tool_calls: int
    tool_errors: int
    # None rather than 0.0 when nothing ran: a rate over zero calls is unknown,
    # and a cell reading 0.0% would be a lie.
    tool_error_rate: Number | None = None
    p50_duration_ms: Number | None = None
    signing_secret_is_development_default: bool


class MetricView(Contract):
    name: str
    value: Number | None = None
    unit: str
    available: bool
    reason: str
    sample_size: int


class OperationalMetrics(Contract):
    """§59, split into what is measured and what cannot be."""
    merchant_id: str
    available: list[MetricView]
    unavailable: list[MetricView]
    note: str


class ObjectiveView(Contract):
    name: str
    target: str
    measured: Number | None = None
    holds: bool | None = None
    detail: str


class Objectives(Contract):
    objectives: list[ObjectiveView]


# ---------------------------------------------------------------- ledger
class IncidentExposure(Contract):
    """One incident's row in the ledger breakdown.

    Declared, rather than left as the `dict` it used to be, because an
    undeclared field is an undeclared TYPE. `recoverable_minor` reached the
    wire as the string `"2798847"` while the identically-named field one level
    up was the integer `2798747`: Postgres returns `numeric` for `SUM()` over a
    bigint, psycopg2 turns that into `Decimal`, and a model that says `dict`
    gives pydantic nothing to coerce it against. Money on a revenue ledger,
    two types, one response.

    Nothing rendered wrong -- `Money` divides by 100 and JavaScript coerces a
    numeric string -- which is exactly why it survived. The first `reduce` over
    these rows would have concatenated instead of adding.
    """
    incident_id: str
    incident_type: str
    severity: str
    status: str
    title: str
    revenue_at_risk_minor: int
    recoverable_minor: int
    recovered_minor: int


class MethodExposure(Contract):
    """One payment method's row in the ledger breakdown. Same story as
    `IncidentExposure`, same fix."""
    method: str
    recoverable_minor: int
    recovered_minor: int
    candidates: int


class LedgerView(Contract):
    """§49's six figures. They nest, and `invariants_broken` is reported rather
    than raised — a ledger whose figures do not nest is a defect that has to be
    visible."""
    merchant_id: str
    basis: str
    at_risk_minor: int
    recoverable_minor: int
    attempted_minor: int
    recovered_minor: int
    failed_minor: int
    unknown_minor: int
    outstanding_minor: int
    by_incident: list[IncidentExposure]
    by_method: list[MethodExposure]
    invariants_broken: list[str]


class IncidentCounts(Contract):
    open: int
    resolved: int
    # Keyed by status value, so the keys are data.
    by_status: dict[str, int]


class AgentActivity(Contract):
    investigations: int
    recommendations: int
    awaiting_approval: int
    escalations: int
    tool_calls: int


class DashboardView(Contract):
    recovery: LedgerView
    incidents: IncidentCounts
    agent_activity: AgentActivity


# --------------------------------------------------------------- webhooks
class WebhookAck(Contract):
    """Always 200 once the delivery is stored, including a refused one: a
    provider retries a non-2xx, and retrying a forgery achieves only load."""
    status: str
    event_id: str | None = None
    stored_id: str | None = None
    note: str | None = None
    reverified: list = []
    incident_id: str | None = None


# ----------------------------------------------------------------- replay
class ReplayStep(Contract):
    seq: int
    tool: str
    arguments: dict | None = None
    success: bool
    error_code: str | None = None
    risk_level: str | None = None
    policy_decision: str | None = None
    duration_ms: int | None = None


class ReplayResult(Contract):
    mode: str
    task_id: str
    request: str
    status: str
    final_answer: str | None = None
    steps: list[ReplayStep]
    trace: list[TraceEvent]
    external_calls_made: int
    note: str | None = None
    # RE_REASON only: how the re-run compared with the recorded one.
    divergence: dict | None = None
    replay_task_id: str | None = None


# --------------------------------------------------------------- recovery
class SettleReport(Contract):
    plan_id: str
    status: str
    expected_recovery_minor: int
    actual_recovery_minor: int
    by_status: dict[str, int]


class DispatchResult(Contract):
    candidate_id: str
    task: TaskView
    risk: dict
    plan: PlanView


# -------------------------------------------------------------- scenarios
class ScenarioView(Contract):
    id: str
    category: str
    critical: bool
    description: str
    request: str
    principal: str
    # `expect` and `setup` are the scenario's own configuration; their keys vary
    # by scenario and belong to the YAML, not to this module.
    expect: dict
    setup: dict


class ScenarioCheck(Contract):
    name: str
    passed: bool
    detail: str


class ScenarioRunResult(Contract):
    scenario_id: str
    passed: bool
    checks: list[ScenarioCheck]
    metrics: dict
    task_id: str | None = None
    provider: str
    model: str

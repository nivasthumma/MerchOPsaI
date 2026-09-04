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
    id: str
    task_id: str
    merchant_id: str
    # What kind of action is stuck. The queue listed identifiers and amounts and
    # left the reader to open each task to find out whether the money in
    # question was a refund going out or a payment link that may never have been
    # sent. It is also what notification routing derives its recipients from.
    action_type: str
    target_payment_id: str | None = None
    external_payment_id: str | None = None
    amount_minor: int | None = None
    external_reference: str | None = None
    verification_state: str | None = None
    verify_attempts: int
    updated_at: str | object = None
    verification_detail: dict | None = None


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
    """MerchantOps v2 §38's five bounds. All five belong to the plan."""
    max_recovery_minor: int
    max_actions: int
    max_attempts_per_customer: int
    max_duration_seconds: int
    max_risk_level: str


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


# ------------------------------------------------------ merchant state (§14)
class MerchantStateView(Contract):
    """MerchantOps v2 §14's MerchantState.

    Branches are `dict` rather than modelled field by field. Their contents are
    assembled from modules that own their own shapes — the ledger, the metrics
    registry, the dashboard — and mirroring those here would create a second
    definition that drifts from the first. That is the same reasoning the module
    docstring gives for typing audit payloads as `dict`.

    A branch that could not be measured carries `measured: false` and a reason
    rather than a zero.
    """
    merchant_id: str
    # Computed per read, so the figures are as of this request. A dashboard
    # number with no as-of is one somebody quotes an hour later.
    as_of: str
    period_days: int
    financial: dict
    payments: dict
    customers: dict
    incidents: dict
    recovery: dict
    operational_health: dict


# --------------------------------------------------------- campaigns (§37)
class CampaignBudget(Contract):
    """§38's five bounds, each beside what has been used against it.

    A limit with no consumption reading is a limit nobody can see approaching,
    which for a merchant watching an ACTIVE campaign is the only thing they
    actually want to know.
    """
    max_recovery_minor: int
    spent_minor: int
    max_actions: int
    actions_taken: int
    max_attempts_per_customer: int
    max_duration_seconds: int
    elapsed_seconds: int
    # §38's fifth bound. It was enforced as a module constant before this;
    # a limit nobody can see on the campaign is not an explicit limit.
    max_risk_level: str


class CampaignView(Contract):
    """MerchantOps v2 §37's card. A projection of a plan, not a second entity."""
    id: str
    incident_id: str
    objective: str
    intervention: str
    status: str

    affected: int
    eligible: int
    ineligible: int
    attempted: int
    recovered: int
    failed: int
    unknown: int
    skipped: int

    revenue_at_risk_minor: int
    eligible_recovery_minor: int
    # An ESTIMATE, returned with its basis so a client cannot render the figure
    # without the reasoning (§49).
    expected_recovery_minor: int
    expected_recovery_basis: str
    # What was actually recovered. A different field from expected, always.
    recovered_minor: int

    budget: CampaignBudget
    # Bounds already used up. Reported, never acted on — the stopping rules are
    # the authority on whether a campaign may continue.
    exhausted: list[str] = []

    stop_rule: str | None = None
    stop_reason: str | None = None
    expires_at: str


class CampaignList(Contract):
    campaigns: list[CampaignView]
    total_expected_recovery_minor: int


# ------------------------------------------------------------- hypotheses
class HypothesisView(Contract):
    """One candidate explanation and how it fared — MerchantOps v2 §30."""
    id: str
    label: str
    key: str
    statement: str
    status: str
    proposed_by: str
    support_count: int
    contradiction_count: int
    # The platform's words for why it landed where it did. A rejected
    # hypothesis with no stated reason is an assertion.
    verdict_reason: str | None = None
    adjudicated_at: str | None = None


class HypothesisSet(Contract):
    incident_id: str
    hypotheses: list[HypothesisView]
    # The sole surviving explanation, or null. Null both when nothing survived
    # and when several did — neither of which is "the first one".
    leading: str | None = None
    # Named rather than left to be counted off the list: a hypothesis nobody
    # could test is a gap in instrumentation, and it should be as visible as
    # the verdicts around it.
    untested: list[str] = []


# ---------------------------------------------------------- evidence graph
class EvidenceEdgeView(Contract):
    """One typed relationship — MerchantOps v2 §32."""
    id: str
    subject: dict
    object: dict
    drawn_by: str
    at: str


class EvidenceGraph(Contract):
    """§32's answer to "why do you believe this?", grouped by relationship.

    Predicates are keys rather than a flat list because the question is asked
    one relationship at a time: what caused it, what it affects, what it
    creates, what supports it. A flat list would put the reader back where the
    evidence table left them.
    """
    incident_id: str
    # {CAUSED_BY: [...], SUPPORTED_BY: [...], ...}. Absent predicates are
    # omitted rather than sent empty: an incident with nothing contradicting it
    # should not render a "contradicted by" heading with nothing under it.
    edges: dict[str, list[EvidenceEdgeView]]
    edge_count: int
    # The same graph as one line per edge, so a reader can see that nothing was
    # added between the graph and any sentence written from it.
    lines: list[str]


# ------------------------------------------------------------- live events
class LiveEventView(Contract):
    """One frame of the live stream — MerchantOps v2 §11's field list, §62's names."""
    id: str
    event: str
    schema_version: str
    tenant_id: str | None = None
    merchant_id: str | None = None
    entity_id: str | None = None
    provider: str | None = None
    incident_id: str | None = None
    task_id: str | None = None
    correlation_id: str | None = None
    occurred_at: str
    payload_hash: str
    payload: dict


class LiveEventList(Contract):
    events: list[LiveEventView]
    # The id to pass back as `after` to continue. Null when nothing was
    # returned, because a cursor of "nothing" is the cursor you already had.
    next_cursor: str | None = None
    # What the drain has not yet delivered. A number that only grows is the
    # symptom of a stopped drain, and it is invisible from the frames alone.
    pending: int


class DrainReport(Contract):
    claimed: int
    published: int
    failed: int


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
    # MerchantOps v2 §33: HIGH | MEDIUM | LOW | INSUFFICIENT, computed by the
    # platform from the evidence. Null before investigation.
    confidence: str | None = None
    started_at: str
    detected_at: str
    resolved_at: str | None = None
    # Detail only.
    confidence_inputs: dict | None = None
    signals: dict | None = None
    evidence: list[dict] | None = None
    recovery: PlanView | None = None
    timeline: list[dict] | None = None
    tasks: list[dict] | None = None
    legal_transitions: list[str] | None = None


class IncidentList(Contract):
    incidents: list[IncidentSummary]
    total_revenue_at_risk_minor: int


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
    # MerchantOps v2 §18: how many independent detection rules saw this
    # episode, this one included. 1 means the signal stands alone.
    corroboration: int = 1


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
    by_incident: list[dict]
    by_method: list[dict]
    invariants_broken: list


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


# ------------------------------------------------------ notifications
class NotifyCounts(Contract):
    created: int
    sent: int
    failed: int
    suppressed: int
    # Not an error. The sweep recomputes the same "expiring soon" on every pass
    # and the UNIQUE constraint refuses the repeat, which is the mechanism
    # working rather than a problem to report.
    duplicate: int


class NotifySweepReport(Contract):
    approvals: NotifyCounts
    escalated: NotifyCounts


class NotificationView(Contract):
    id: str
    kind: str
    severity: str
    subject_type: str
    subject_id: str
    recipient: str
    channel: str
    title: str
    status: str
    attempts: int
    last_error: str | None = None
    created_at: str
    sent_at: str | None = None


class NotificationList(Contract):
    notifications: list[NotificationView]
    #: Recorded and never delivered — PENDING or FAILED. The number worth
    #: looking at: it counts people who were not told.
    undelivered: int
    #: What this deployment can actually send on. A deployment that believes it
    #: is emailing and is only writing to a log should be able to find that out
    #: without sending a test approval.
    channels: list[str]

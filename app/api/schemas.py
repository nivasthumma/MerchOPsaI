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

## Where a `dict` survives, and why

ADR-0032 replaced bare-dict ROUTES with response models. It did not reach
inside them, and 31 fields stayed loosely typed. Two of those hid a real
defect: `by_incident` and `by_method` were `list[dict]`, so money reached the
wire as a string in the only two places on the ledger without a declared
integer behind it.

The rest were audited on 2026-09-08 and split three ways.

**Typed, because the shape is knowable:** the evidence rows (`EvidenceRow`),
the incident timeline (`TimelineEntry` / `TimelineDetail`, whose keys the API
filters against a closed list before returning them), the ledger breakdowns
(`IncidentExposure` / `MethodExposure`), and the bare `list`s that meant
"array of anything" in the schema. Typing the timeline immediately caught a
wrong guess -- `detail` is a dict, never a string -- which is the argument in
miniature: a declared type fails at the boundary, an absent one fails in a
consumer.

**Open by nature, and left open deliberately:** `TraceEvent.payload` and
`ActivityEvent.payload` (an audit payload's shape is the event's, and there are
dozens), `ToolCallView.arguments` and `.data` (per-tool by design -- a refund
result and a metrics read share nothing), `IncidentSummary.signals` (each
detection rule emits its own, which is why §12 names the canonical four rather
than a schema), `ScenarioView.expect` / `.setup` (scenario YAML), and
`SavedView.filter`. Declaring these would mean inventing a union that grows
with every new event, tool or rule, and a schema that lies about being closed
is worse than one that admits it is open.

**Empirically checked rather than reasoned about:** every GET endpoint was
walked and scanned for a value that is a string but reads as a number -- the
signature of the money bug. Zero across thirteen endpoints, so that class is
closed rather than presumed closed.
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

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
class EvidenceRow(Contract):
    """One piece of evidence, as `app/tools/contracts.Evidence` produces it.

    Declared because `evidence: list` -- a bare list -- says "array of
    anything" in the OpenAPI document, so a client generates `unknown[]` and
    the compile-time contract check has nothing to check. `untrusted` in
    particular is the §36 injection tag, and a consumer that cannot see it in
    the schema cannot be expected to honour it.

    `value` stays `Any` on purpose: it is a tool's finding, and a rate, a
    count, an id and a free-text string are all legitimate. That is an open
    field by nature rather than one nobody got round to.
    """
    key: str
    value: Any = None
    source: str
    untrusted: bool = False
    # OBSERVED | DERIVED | INFERRED | RECOMMENDED | EXECUTED | VERIFIED
    kind: str = "OBSERVED"
    id: str | None = None


class TimelineDetail(Contract):
    """The keys an incident timeline entry may carry.

    A closed set, and not a guess at one: `app/api/main.py` filters the audit
    payload against exactly this list before it reaches the response. Declaring
    the fields rather than the dict is what makes the OpenAPI document say what
    a timeline entry can actually contain.
    """
    at: str | None = None
    to: str | None = None
    reason: str | None = None
    decision: str | None = None
    rule: str | None = None
    plan_id: str | None = None
    intervention: str | None = None
    state: str | None = None
    status: str | None = None
    # `from` is a Python keyword, so the field is named for the wire and
    # accessed through the alias. Renaming the wire key instead would change
    # the contract to suit the implementation language.
    from_: str | None = Field(default=None, alias="from")

    model_config = ConfigDict(extra="forbid", populate_by_name=True,
                              serialize_by_alias=True)


class TimelineEntry(Contract):
    """One row of an incident's timeline. Every entry is a recorded event with
    its own timestamp -- never an inferred step.

    `detail` was annotated `str | None` on the first attempt at typing this,
    and the suite failed immediately with
    `input: {'rule': 'success_rate_below_baseline'}`. It is a dict, always,
    and that is the argument for declaring these fields rather than leaving
    them as `dict`: a wrong type fails loudly at the boundary, where an absent
    one fails silently in a consumer.
    """
    at: str
    event: str
    detail: TimelineDetail = TimelineDetail()
    task_id: str | None = None


class FailureClassView(Contract):
    """MerchantOps §56. A code says what broke; this says whether trying again
    is even the question."""
    error_code: str
    category: str
    retryability: RetryabilityLiteral
    owning_subsystem: str
    recommended_next_action: str
    correlation_id: str | None
    evidence: list[EvidenceRow] = []
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
    # A hash of the settings that governed the run (app/agent/provenance.py).
    configuration: str | None = None


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
    # The policy snapshot the approval was requested under. Null on approvals
    # created before snapshots were recorded -- never back-filled with a guess.
    policy_version: str | None = None
    policy_decision: str | None = None
    policy_rule: str | None = None


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
    # AI_SUCCESS | AI_FAILED_FALLBACK | AI_UNAVAILABLE_FALLBACK | DETERMINISTIC_ONLY
    # (app/agent/provenance.py). Null for runs recorded before it existed.
    ai_mode: str | None = None
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
    # HUMAN | AGENT | WORKER | WEBHOOK | SYSTEM, and which one (app/context.py).
    actor_type: str | None = None
    actor: str | None = None
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
    evidence: list[EvidenceRow] = []
    # A tool's own normalised output. Shape is per-tool by design -- a
    # refund result and a metrics read have nothing in common -- so this
    # stays open, and says so rather than looking unfinished.
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
    # What kind of action is stuck. The queue listed identifiers and amounts and
    # left the reader to open each task to find out whether the money in
    # question was a refund going out or a payment link that may never have been
    # sent. It is also what notification routing derives its recipients from,
    # so it stays required: `agent_actions.action_type` is NOT NULL and every
    # row this model is built from comes from that table.
    action_type: str
    # The action's own status, alongside the verification state. An action
    # PENDING with no verification state yet is unsettled work too, and the
    # queue has to be able to say which.
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
    failed: int


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
    failed: list[ActionRow]
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
    # `recovered_minor`, by what the money was. Only a payment the provider
    # reports as captured against a recovery link is recovered REVENUE; a
    # verified refund is money returned to a customer to correct a charge.
    recovered_captured_minor: int = 0
    recovered_refunded_minor: int = 0


class AgentPosture(Contract):
    """Which reasoning is configured, and how recent runs were actually
    produced. `runs_by_mode` is counted from the tasks, not inferred from
    configuration: a model can be configured and still be falling back."""
    provider: str
    model: str | None = None
    fallback_enabled: bool
    runs_by_mode: dict[str, int]


class ProviderPosture(Contract):
    """Which provider universe actions go to, and whether its deliveries are
    being processed. `adapter_mode` is `mock` unless real credentials exist --
    a mocked provider is never described as real (CONTRACT §7)."""
    adapter_mode: str
    live: bool
    webhook_signature_verification: bool
    webhooks_pending: int
    webhooks_dead_lettered: int
    last_webhook_at: str | None = None


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
    agent: AgentPosture
    provider: ProviderPosture


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


class WebhookProcessReport(Contract):
    """One pass of asynchronous webhook processing (app/webhooks/processing.py)."""
    processed: int
    failed: int
    dead_lettered: int


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
    evidence: list[EvidenceRow] | None = None
    recovery: PlanView | None = None
    timeline: list[TimelineEntry] | None = None
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


class QueueView(Contract):
    """The task queue, and whether anything is draining it."""
    queued: int
    running: int
    #: How long the oldest queued task has waited. Depth alone cannot say a
    #: queue is stuck -- a deep queue that is moving is healthy, and one task
    #: that has waited an hour is not.
    oldest_queued_seconds: int | None = None
    worker_seen_seconds_ago: int | None = None
    #: False means nothing is running the sweeps or the queue. Until this
    #: existed the absence of work looked exactly like there being no work.
    worker_is_live: bool


class SharedState(Contract):
    """Whether state that must agree across replicas actually does."""
    #: `shared` | `degraded` | `process`. `degraded` means Redis is configured
    #: and not answering, so the limiter has fallen back per-process -- a state
    #: worth seeing, because nothing else about the response reveals it.
    backend: str
    rate_limit_scope: str
    provider_override_scope: str


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
    #: True means pre-ADR-0049 tokens -- which never expire and cannot be
    #: revoked individually -- are still accepted. Intended for the length of a
    #: rollout and not beyond it.
    auth_accepts_legacy_tokens: bool
    auth_access_token_seconds: int
    webhook_signature_verification: bool
    agent_budget: AgentBudget
    #: Whether the rate limiter and the provider override are shared across
    #: replicas. A deployment running three API processes with no Redis is
    #: serving three times its configured rate limit, and nothing else in this
    #: response would say so.
    shared_state: SharedState
    #: How tasks run here, and whether a worker is alive to run the queued ones.
    agent_execution_mode: str
    queue: QueueView


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
    #: `fleet` when the override reached shared state and every replica will
    #: honour it, `this_replica_only` when there is no shared backend and it
    #: applied to the process that served this request. Different outcomes, and
    #: an operator watching the provider they just turned off keep being used is
    #: entitled to know which one happened.
    applies_to: str = "this_replica_only"


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
    # `recovered_minor` by what the money was: captured against a recovery
    # link (revenue) or a verified refund (money returned). See the ledger.
    recovered_captured_minor: int = 0
    recovered_refunded_minor: int = 0
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
    reverified: list[str] = []
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


class InstanceReadiness(Contract):
    """`/ready` — whether this instance can do work, not whether it is alive.

    Named apart from `Readiness` because both branches of the spine/trunk merge
    defined a model by that name and they are not the same shape: this one is
    `/ready`'s two-field answer, and `Readiness` is `/readiness`'s per-component
    report. Sharing the name meant the later definition silently won and the
    other endpoint returned a 500 on response validation.
    """
    ready: bool
    #: {"database": {"ok": ...}, "schema": {"ok": ..., "at": ..., "expected": ...}}
    #: Shaped as a dict rather than modelled per check so a new check does not
    #: need a schema change and a frontend regeneration to be reportable.
    checks: dict


# ------------------------------------------------- provider health (§26)
class ProviderOperation(Contract):
    """One kind of call, and how it has actually gone."""
    action_type: str
    attempted: int
    succeeded: int
    failed: int
    #: Neither. The number this system refuses to fold into either column.
    unknown: int
    p50_latency_ms: int | None = None
    p95_latency_ms: int | None = None


class ProviderDay(Contract):
    day: str
    attempted: int
    succeeded: int
    failed: int
    unknown: int


class ProviderHealth(Contract):
    #: The current verdict, from the same check `/readiness` reports.
    status: str
    detail: str
    mode: str
    environment: str
    execution_is_real: bool
    #: Webhook deliveries, which is the provider talking to us rather than the
    #: reverse — a provider can be healthy outbound and silent inbound.
    webhooks_received: int
    webhooks_rejected: int
    operations: list[ProviderOperation]
    history: list[ProviderDay]


# ------------------------------------------------------ audit search (§28)
class AuditEntry(Contract):
    id: int
    event_type: str
    created_at: str
    merchant_id: str | None = None
    user_id: str | None = None
    task_id: str | None = None
    incident_id: str | None = None
    correlation_id: str | None = None
    payload: dict | list | str | None = None


class AuditPage(Contract):
    entries: list[AuditEntry]
    #: What the filter matched, not what this page holds. A count taken from
    #: the page is the same defect this repository has written three times.
    matched: int
    #: Pass back as `before_id` for the next page. Null when there is no more.
    next_cursor: int | None = None
    #: Distinct event types present, so the filter can be built from what is
    #: there rather than from a list somebody maintains.
    event_types: list[str] = []


# ------------------------------------------------------ policy admin (§41)
class PolicyControl(Contract):
    """One thing policy decides, and whether a merchant may change it."""
    key: str
    label: str
    #: What applies to this merchant right now.
    effective: int | str | bool
    #: The platform default, so an override is visible AS an override.
    default: int | str | bool
    #: True when this merchant has set its own value.
    overridden: bool
    #: False for the controls that are not per-merchant. Listed anyway, because
    #: a policy screen showing only the editable third of policy invites the
    #: reader to think that is all of it.
    editable: bool
    why: str


class PolicyView(Contract):
    merchant_id: str
    controls: list[PolicyControl]


class SetPolicyRequest(Contract):
    #: Paise. None clears the override and returns the merchant to the default.
    refund_limit_minor: int | None = None


class PolicyChange(Contract):
    merchant_id: str
    key: str
    before: int | None = None
    after: int | None = None
    changed: bool


# ------------------------------------------------------ onboarding (§45)
class CreateMerchantRequest(Contract):
    name: str
    #: Optional. Generated when absent, so the common case is one field.
    merchant_id: str | None = None
    currency: str = "INR"


class MerchantView(Contract):
    merchant_id: str
    tenant_id: str
    name: str
    currency: str


class MerchantList(Contract):
    merchants: list[MerchantView]


# ------------------------------------------------------ access review
class RoleView(Contract):
    name: str
    permissions: list[str]


class AccessReviewEntry(Contract):
    user_id: str
    email: str
    merchant_id: str
    role: str
    permissions: list[str]
    #: ACTIVE | DISABLED. Offboarded accounts are listed deliberately: "whose
    #: access was removed, and when" is half of what a review asks.
    status: str
    deactivated_at: str | None = None


class AccessReview(Contract):
    """§66's answer to "who can do what", as something a person can sign off."""
    tenant_id: str
    #: Computed per read. An access review quoted without an as-of is one
    #: somebody attests to a week after it stopped being true.
    generated_at: str
    roles: list[RoleView]
    users: list[AccessReviewEntry]


# ------------------------------------------------------ people (ADR-0048)
class CreateUserRequest(Contract):
    email: str
    #: The role's NAME, not its id. An id is a value a caller could copy from
    #: another tenant's response; a name that does not exist here is a clean 404.
    role: str


class UpdateUserRequest(Contract):
    role: str | None = None
    #: ACTIVE | DISABLED. Offboarding is a status change, never a delete: the
    #: audit trail points at this row and has to outlive the employment.
    status: str | None = None


class UserSummary(Contract):
    user_id: str
    email: str
    role: str
    status: str
    permissions: list[str]


class UserList(Contract):
    users: list[UserSummary]


class UserCreated(Contract):
    user_id: str
    email: str
    role: str
    #: Returned ONCE and never stored. Authentication is an HMAC of the user id,
    #: so there is no password to set and no acceptance step -- creating the user
    #: is granting the credential.
    token: str


class UserChange(Contract):
    user_id: str
    role: str | None = None
    status: str | None = None
    changed: bool | None = None


class CreateRoleRequest(Contract):
    name: str
    description: str | None = None
    permissions: list[str] = []


class SetPermissionsRequest(Contract):
    permissions: list[str]


class RoleSummary(Contract):
    name: str
    description: str
    permissions: list[str]
    #: How many users hold it. A role nobody holds is a role that can be deleted;
    #: one that everybody holds is a role worth looking at.
    held_by: int


class PermissionView(Contract):
    name: str
    description: str


class RoleList(Contract):
    roles: list[RoleSummary]
    #: Every permission that exists, derived from the tool registry. Sent
    #: alongside so a client building a role picker does not have to guess.
    catalogue: list[PermissionView]


class RoleChange(Contract):
    name: str
    permissions: list[str]
    granted: list[str]
    revoked: list[str]


# ------------------------------------------------------ tokens (ADR-0049)
class RefreshRequest(Contract):
    refresh_token: str


class TokenPair(Contract):
    access_token: str
    #: A NEW one. The presented refresh token is revoked by the exchange:
    #: single use, so a replay is detectable and means somebody has a copy.
    refresh_token: str
    expires_in: int
    token_type: str = "Bearer"  # noqa: S105 - the scheme name, not a secret


class SignOutResult(Contract):
    #: `this_session` or `all_sessions`.
    signed_out: str


# ------------------------------------------------------ SSO (ADR-0050)
class SsoStart(Contract):
    authorization_url: str
    state: str


class SsoCallback(Contract):
    #: Exchanged for a token pair at `/auth/sso/exchange`. Single use, and
    #: short-lived: it exists so no credential ever travels in a URL.
    handoff_code: str
    redirect_to: str
    #: True when this sign-in created the account rather than matching one.
    provisioned: bool


class SsoExchangeRequest(Contract):
    handoff_code: str


class SsoConfigRequest(Contract):
    issuer: str
    client_id: str
    client_secret: str
    #: Which email domains route a sign-in to this tenant. The only fact a
    #: sign-in box has before anybody is authenticated.
    email_domains: list[str]
    #: The role a first-time user gets. Never `owner`.
    default_role: str = "analyst"
    default_merchant_id: str | None = None
    enabled: bool = True


class SsoConfig(Contract):
    configured: bool
    issuer: str | None = None
    client_id: str | None = None
    #: The client secret is never returned. It goes in and does not come out.
    email_domains: list[str] = []
    default_role: str | None = None
    default_merchant_id: str | None = None
    enabled: bool | None = None


# ------------------------------------------------------ SCIM (ADR-0051)
class CreateScimTokenRequest(Contract):
    name: str | None = None
    default_role: str = "analyst"
    default_merchant_id: str | None = None


class ScimTokenCreated(Contract):
    id: str
    #: Shown once. Stored as a SHA-256 hash, so it cannot be shown again.
    token: str
    name: str


class ScimTokenSummary(Contract):
    id: str
    name: str
    default_merchant_id: str
    default_role: str
    created_at: str
    #: Answers "is the integration actually running?" — which is the question
    #: asked when somebody's offboarding did not take effect.
    last_used_at: str | None = None
    revoked: bool


class ScimTokenList(Contract):
    tokens: list[ScimTokenSummary]


class ScimTokenRevoked(Contract):
    id: str
    revoked: bool

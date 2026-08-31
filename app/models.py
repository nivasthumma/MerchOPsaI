"""SQLAlchemy schema — CONTRACT §42 (as amended by ADR-0008)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, func, Boolean, DateTime, Enum, ForeignKey, Index, Integer,
    JSON, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Enumerations (CONTRACT §19, §20, §26, §34)
# --------------------------------------------------------------------------
class RiskLevel(str, enum.Enum):
    """MerchantOps §24. Ordered — `RISK_ORDER` below is what makes the risk
    engine's floor rule expressible: computed risk may raise, never lower."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Ordinal, not alphabetical. Comparing risk by string would put CRITICAL below
# HIGH and silently invert the one rule the risk engine exists to enforce.
RISK_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


def risk_at_least(a: str, b: str) -> str:
    """The higher of two risk levels."""
    return a if RISK_ORDER.get(a, 0) >= RISK_ORDER.get(b, 0) else b


class PolicyDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    REQUIRE_DUAL_APPROVAL = "REQUIRE_DUAL_APPROVAL"


class VerificationState(str, enum.Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    DENIED = "DENIED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    ABORTED_BUDGET = "ABORTED_BUDGET"


class ActionStatus(str, enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class FailureCode(str, enum.Enum):
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    TOOL_INVALID_ARGUMENT = "TOOL_INVALID_ARGUMENT"
    TOOL_UNAVAILABLE = "TOOL_UNAVAILABLE"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"
    POLICY_DENIED = "POLICY_DENIED"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    APPROVAL_EXPIRED = "APPROVAL_EXPIRED"
    EXTERNAL_API_ERROR = "EXTERNAL_API_ERROR"
    EXTERNAL_STATE_UNKNOWN = "EXTERNAL_STATE_UNKNOWN"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    PARTIAL_EXECUTION = "PARTIAL_EXECUTION"
    MODEL_INVALID_OUTPUT = "MODEL_INVALID_OUTPUT"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    REPLAY_DIVERGED = "REPLAY_DIVERGED"


class IncidentType(str, enum.Enum):
    """MerchantOps §12 — what the detection engine found."""
    PAYMENT_DEGRADATION = "PAYMENT_DEGRADATION"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    # MerchantOps §35: internal state and provider state disagree. Not an
    # anomaly in the merchant's business — an anomaly in our own record of it,
    # which is why it is raised rather than silently corrected.
    RECONCILIATION_MISMATCH = "RECONCILIATION_MISMATCH"


class Intervention(str, enum.Enum):
    """MerchantOps §23 — what may be done about an affected transaction."""
    RETRY = "RETRY"
    PAYMENT_LINK = "PAYMENT_LINK"
    CUSTOMER_NOTIFICATION = "CUSTOMER_NOTIFICATION"
    SUBSCRIPTION_RETRY = "SUBSCRIPTION_RETRY"
    REFUND = "REFUND"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"
    NO_ACTION = "NO_ACTION"


class PlanStatus(str, enum.Enum):
    """MerchantOps §28 — stopping is a first-class state, not an absence of work."""
    DRAFT = "DRAFT"            # candidates computed, nothing executed
    ACTIVE = "ACTIVE"          # at least one candidate has been acted on
    STOPPED = "STOPPED"        # a stopping rule fired; no further action
    ESCALATED = "ESCALATED"    # a stopping rule fired that needs a human
    COMPLETED = "COMPLETED"    # every eligible candidate resolved
    EXPIRED = "EXPIRED"        # §27 maximum duration elapsed


class CandidateStatus(str, enum.Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    ATTEMPTED = "ATTEMPTED"
    RECOVERED = "RECOVERED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SKIPPED = "SKIPPED"        # dropped by budget or a stopping rule


class WebhookStatus(str, enum.Enum):
    """MerchantOps §34. Every delivery lands in exactly one of these, including
    the ones we refuse — a rejected webhook that leaves no row is a delivery
    nobody can investigate."""
    RECEIVED = "RECEIVED"        # stored, not yet processed
    PROCESSED = "PROCESSED"      # acted on (verification re-run)
    IGNORED = "IGNORED"          # valid, but nothing here subscribes to it
    DUPLICATE = "DUPLICATE"      # event_id already seen
    INVALID = "INVALID"          # signature failed


class IncidentSeverity(str, enum.Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class IncidentStatus(str, enum.Enum):
    """MerchantOps §13 canonical lifecycle, plus the terminal/exception set.

    The order of the canonical states is meaningful: `app.incidents.lifecycle`
    derives the legal forward transitions from it. Terminal states are listed
    separately because they are reachable from anywhere, not from a predecessor.
    """
    DETECTED = "DETECTED"
    TRIAGED = "TRIAGED"
    INVESTIGATING = "INVESTIGATING"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    RECOVERY_PLANNED = "RECOVERY_PLANNED"
    POLICY_EVALUATING = "POLICY_EVALUATING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    # exception / terminal (MerchantOps §13)
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


# --------------------------------------------------------------------------
# Business entities
# --------------------------------------------------------------------------
class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    policy_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    email: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(64))          # owner | analyst | support
    permissions: Mapped[list] = mapped_column(JSON, default=list)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(200))
    segment: Mapped[str] = mapped_column(String(64), default="standard")
    # MerchantOps §28 makes "customer has opted out" a stopping condition. It has
    # to be a fact the planner can read, not a policy someone remembers.
    contact_opted_out: Mapped[bool] = mapped_column(Boolean, default=False)
    # CONTRACT §36: free-text merchant data is an injection surface.
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Product(Base):
    __tablename__ = "products"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(64))
    price_minor: Mapped[int] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.id"))
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(32))        # created | paid | failed
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class Payment(Base):
    """CONTRACT §6/§42 — carries the synthetic→external mapping."""
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    method: Mapped[str] = mapped_column(String(32), index=True)   # upi | card | netbanking | wallet
    status: Mapped[str] = mapped_column(String(32), index=True)   # captured | failed | refunded
    error_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount_refunded_minor: Mapped[int] = mapped_column(Integer, default=0)
    refund_status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # null|partial|full
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- external mapping layer (CONTRACT §6) ---
    external_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    __table_args__ = (Index("ix_payments_merchant_created", "merchant_id", "created_at"),)


class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))        # processed | pending | failed
    external_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# --------------------------------------------------------------------------
# Provider events (MerchantOps §11, §34)
# --------------------------------------------------------------------------
class WebhookEvent(Base):
    """The durable event store — MerchantOps §11's field list, §34's pipeline.

    A webhook is **evidence, not authority**. This row records that a provider
    said something; it never decides what is true. Deciding is reconciliation's
    job, and reconciliation re-reads provider state through the adapter rather
    than believing this payload (MerchantOps §32: a delivered message is not
    verified business state, for the same reason an HTTP 200 is not).

    Rows are written for rejected deliveries too. A webhook refused for a bad
    signature that leaves no trace is an attack nobody can investigate.
    """
    __tablename__ = "webhook_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # The provider's own event id. UNIQUE is the deduplication mechanism
    # (MerchantOps §34): a redelivered webhook collides here and is recorded as
    # DUPLICATE rather than processed twice. Providers retry by design, so this
    # is the ordinary path, not the exceptional one.
    event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)

    provider: Mapped[str] = mapped_column(String(32), default="razorpay", index=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="v1")

    # Nullable: the event may name an entity this system has never seen, and
    # inventing a merchant for it would be worse than admitting we cannot place it.
    merchant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    status: Mapped[WebhookStatus] = mapped_column(
        Enum(WebhookStatus, native_enum=False), default=WebhookStatus.RECEIVED, index=True)
    signature_valid: Mapped[bool] = mapped_column(Boolean, default=False)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # Hash of the exact bytes the signature was computed over, so a stored event
    # can be tied back to what was actually delivered.
    payload_hash: Mapped[str] = mapped_column(String(64))

    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_webhook_entity_type", "entity_id", "event_type"),)


# --------------------------------------------------------------------------
# Incidents (MerchantOps §12, §13)
# --------------------------------------------------------------------------
class Incident(Base):
    """A detected, significant problem. MerchantOps §13.

    Incidents are created by `app.detection`, never by the model. The model
    investigates an incident that already exists; it cannot declare one, close
    one, or move one through its lifecycle. Every transition runs through
    `app.incidents.lifecycle`, which is deterministic control-plane logic.
    """
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)

    incident_type: Mapped[IncidentType] = mapped_column(Enum(IncidentType, native_enum=False))
    severity: Mapped[IncidentSeverity] = mapped_column(Enum(IncidentSeverity, native_enum=False))
    status: Mapped[IncidentStatus] = mapped_column(
        Enum(IncidentStatus, native_enum=False), default=IncidentStatus.DETECTED, index=True)

    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)

    # Detection is idempotent through this key, exactly as execution is
    # idempotent through agent_actions.idempotency_key. Re-running the sweep
    # over the same window must not manufacture a second incident for one
    # anomaly -- an operations console that grows a new HIGH incident on every
    # detection pass is worse than no console.
    detection_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    # MerchantOps §22: the number is owned by the calculation engine. It is
    # written here by deterministic code and is never model output.
    revenue_at_risk_minor: Mapped[int] = mapped_column(Integer, default=0)

    # The metrics that tripped the rule: baseline, observed, threshold, window.
    # Kept so an operator can see *why* this was called an anomaly.
    signals: Mapped[dict] = mapped_column(JSON, default=dict)

    detection_rule: Mapped[str] = mapped_column(String(64))
    detection_version: Mapped[str] = mapped_column(String(32))
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    evidence: Mapped[list["IncidentEvidence"]] = relationship(
        back_populates="incident", order_by="IncidentEvidence.id")

    __table_args__ = (Index("ix_incidents_merchant_status", "merchant_id", "status"),)


class IncidentEvidence(Base):
    """MerchantOps §36 — every conclusion carries its evidence.

    Mirrors the `Evidence` tool contract, including the `untrusted` tag: an
    incident summarising customer free text must carry the same quarantine
    marker a tool result would (MerchantOps §39).
    """
    __tablename__ = "incident_evidence"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[dict] = mapped_column(JSON)
    source: Mapped[str] = mapped_column(String(64))
    untrusted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


# --------------------------------------------------------------------------
# Recovery planning (MerchantOps §23, §27, §28)
# --------------------------------------------------------------------------
class RecoveryPlan(Base):
    """What could be done about an incident, and the bounds on doing it.

    MerchantOps §23 ends at *intervention candidates* -- planning does not
    execute. Execution remains §29's existing path, one action at a time,
    through the same policy, approval, idempotency and verification gates every
    other financial action goes through. There is deliberately no second way to
    move money.

    The budget (§27) is copied onto the plan at creation rather than read live.
    A campaign's bounds are part of the decision that authorised it; a merchant
    raising their limit mid-campaign must not silently widen a plan already in
    flight.
    """
    __tablename__ = "recovery_plans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)

    status: Mapped[PlanStatus] = mapped_column(
        Enum(PlanStatus, native_enum=False), default=PlanStatus.DRAFT, index=True)
    intervention: Mapped[Intervention] = mapped_column(Enum(Intervention, native_enum=False))

    # One plan per incident. A second planning pass must refine the existing
    # plan, not open a parallel campaign against the same incident with its own
    # separate budget -- which is how a bounded campaign becomes unbounded.
    plan_key: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)

    # --- §22: computed, never model output ---
    revenue_at_risk_minor: Mapped[int] = mapped_column(Integer, default=0)
    eligible_recovery_minor: Mapped[int] = mapped_column(Integer, default=0)
    expected_recovery_minor: Mapped[int] = mapped_column(Integer, default=0)
    expected_recovery_basis: Mapped[str] = mapped_column(Text, default="")

    # --- §27: the bounds ---
    max_recovery_minor: Mapped[int] = mapped_column(Integer)
    max_actions: Mapped[int] = mapped_column(Integer)
    max_attempts_per_customer: Mapped[int] = mapped_column(Integer)
    max_duration_seconds: Mapped[int] = mapped_column(Integer)

    # --- §28: why it stopped, if it did ---
    stop_rule: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    planner_version: Mapped[str] = mapped_column(String(32))
    correlation_id: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    candidates: Mapped[list["RecoveryCandidate"]] = relationship(
        back_populates="plan", order_by="RecoveryCandidate.rank")


class RecoveryCandidate(Base):
    """One transaction the plan could act on, and what acting would be worth.

    `expected_recovery_minor` is an ESTIMATE with a stated basis, never a
    promise. §49 keeps expected and actual recovery in separate columns for
    exactly this reason, and nothing in this system may report the two as one
    number.
    """
    __tablename__ = "recovery_candidates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(ForeignKey("recovery_plans.id"), index=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)

    payment_id: Mapped[str] = mapped_column(ForeignKey("payments.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)

    intervention: Mapped[Intervention] = mapped_column(Enum(Intervention, native_enum=False))
    status: Mapped[CandidateStatus] = mapped_column(
        Enum(CandidateStatus, native_enum=False), default=CandidateStatus.ELIGIBLE, index=True)
    ineligible_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    expected_recovery_minor: Mapped[int] = mapped_column(Integer, default=0)
    actual_recovery_minor: Mapped[int] = mapped_column(Integer, default=0)

    # Whether an executable tool exists for this intervention today. A candidate
    # for an intervention with no tool is a real recommendation, not a bug --
    # but it must never be counted as actionable.
    executable: Mapped[bool] = mapped_column(Boolean, default=False)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # The task this candidate was dispatched as, if it has been. Dispatch goes
    # through the ordinary agent path, so the candidate's outcome is read back
    # from that task's action rather than tracked separately.
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plan: Mapped[RecoveryPlan] = relationship(back_populates="candidates")
    __table_args__ = (
        UniqueConstraint("plan_id", "payment_id", name="uq_candidate_plan_payment"),
        Index("ix_candidate_plan_status", "plan_id", "status"),
    )


# --------------------------------------------------------------------------
# Agent execution
# --------------------------------------------------------------------------
class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    request: Mapped[str] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus, native_enum=False), default=TaskStatus.PENDING)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    findings: Mapped[list] = mapped_column(JSON, default=list)      # CONTRACT §14 Finding[]
    recommendation: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agent_version: Mapped[str] = mapped_column(String(64))
    model_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    scenario_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The incident this task investigates, when it was dispatched by one
    # (MerchantOps §13). Null for a task a user started by asking a question --
    # both entry points are legitimate and the loop is the same after this point.
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True)
    is_replay: Mapped[bool] = mapped_column(Boolean, default=False)
    replayed_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="task", order_by="ToolCall.seq")


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    tool_name: Mapped[str] = mapped_column(String(64))
    input: Mapped[dict] = mapped_column(JSON)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    risk_level: Mapped[str | None] = mapped_column(String(16), nullable=True)
    policy_decision: Mapped[str | None] = mapped_column(String(32), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    injected_fault: Mapped[str | None] = mapped_column(String(64), nullable=True)  # §35A
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    task: Mapped[AgentTask] = relationship(back_populates="tool_calls")
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_toolcall_task_seq"),)


class AgentAction(Base):
    """CONTRACT §24 + ADR-0008 #2. The action-ATTEMPT record.

    Reserved (INSERT) before any external call. The UNIQUE constraint on
    idempotency_key is what makes duplicate execution impossible and
    UNKNOWN reconciliation possible.
    """
    __tablename__ = "agent_actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    target_payment_id: Mapped[str] = mapped_column(String(64))
    external_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[ActionStatus] = mapped_column(Enum(ActionStatus, native_enum=False), default=ActionStatus.PENDING)
    external_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_state: Mapped[VerificationState | None] = mapped_column(
        Enum(VerificationState, native_enum=False), nullable=True
    )
    verification_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    verify_attempts: Mapped[int] = mapped_column(Integer, default=0)
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The candidate this execution came from, when it came from a plan. There is
    # no separate recovery_actions table on purpose: agent_actions is where
    # idempotency and verification live, and a second execution record would be
    # a second authority on whether money moved (MerchantOps §67).
    recovery_candidate_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(32))
    action_payload: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str] = mapped_column(String(32), default="PENDING")  # PENDING|APPROVED|REJECTED|EXPIRED
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    # MerchantOps §25 REQUIRE_DUAL_APPROVAL. 1 for an ordinary approval, 2 when
    # policy demanded a second pair of eyes. Stored on the record rather than
    # re-derived at execution: the requirement is a property of the decision
    # that was made when the action was proposed, and a later policy change must
    # not quietly reduce what an in-flight action needs.
    required_signatures: Mapped[int] = mapped_column(Integer, default=1)

    signatures: Mapped[list["ApprovalSignature"]] = relationship(
        back_populates="approval", order_by="ApprovalSignature.signed_at")


class ApprovalSignature(Base):
    """One human's decision on one approval — MerchantOps §26.

    The UNIQUE constraint is the whole design. "Two approvers" enforced by an
    if-statement is a check that can be bypassed by a retry, a race, or a later
    refactor that forgets it. Enforced by the database, one person signing twice
    is not a case the application has to remember to reject -- it is a write
    that cannot succeed.
    """
    __tablename__ = "approval_signatures"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))          # APPROVED | REJECTED
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    approval: Mapped[Approval] = relationship(back_populates="signatures")
    __table_args__ = (
        UniqueConstraint("approval_id", "user_id", name="uq_signature_approval_user"),
    )


class AuditLog(Base):
    """CONTRACT §27 — append-only from the application's perspective."""
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    # MerchantOps §47 requires incident_id on the audit event. Detection and
    # lifecycle transitions happen with no task in scope, so an audit trail
    # keyed only on task_id could not record them at all.
    incident_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    merchant_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    # server_default: an append-only audit trail must not depend on the
    # application for its timestamps. The database stamps every row.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True)


class EvaluationResult(Base):
    __tablename__ = "evaluation_results"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    scenario_id: Mapped[str] = mapped_column(String(64), index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

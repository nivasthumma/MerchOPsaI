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
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PolicyDecision(str, enum.Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


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

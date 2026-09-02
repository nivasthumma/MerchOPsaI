"""SQLAlchemy schema — CONTRACT §42 (as amended by ADR-0008)."""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger, func, Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer,
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
    # MerchantOps §56. Distinct from MODEL_INVALID_OUTPUT: the output parsed and
    # matched the schema, and then cited evidence that does not exist. A
    # well-formed claim about nothing is a different defect from malformed JSON,
    # and collapsing the two would hide the more interesting one.
    AGENT_GROUNDING_FAILURE = "AGENT_GROUNDING_FAILURE"
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
    # MerchantOps §11/§12. Found in the EVENT STORE rather than in payment
    # history: the provider is telling us about failures faster than they land
    # on rows we own, and a burst of them is a signal in its own right.
    PROVIDER_FAILURE_BURST = "PROVIDER_FAILURE_BURST"


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
    """The incident lifecycle — MerchantOps §13, extended to v2 §20.

    The order of the canonical states is meaningful: `app.incidents.lifecycle`
    derives the legal forward transitions from it. Terminal states are listed
    separately because they are reachable from anywhere, not from a predecessor.

    ## Every state here is one the code actually reaches

    v2 §20 lists seventeen canonical states. Five were genuinely missing and are
    added below. Two belong to a different entity, and one has no moment on this
    entity at all — see ADR-0039. A state nothing ever transitions through is
    worse than a smaller honest machine: no scenario can grade it, and a
    merchant reading a status list finds half its entries unreachable.

    ## v2 §20 crosswalk

        v2                     here
        RECEIVED               webhook_events.status — the EVENT's lifecycle
        VALIDATING             webhook_events — signature validation, §34
        DETECTED               DETECTED
        TRIAGING               TRIAGED
        INVESTIGATING          INVESTIGATING
        EVIDENCE_COLLECTING    EVIDENCE_COLLECTING      (new)
        DIAGNOSING             DIAGNOSING               (new)
        IMPACT_CALCULATING     — computed by detection, before the incident row
        RECOVERY_PLANNING      RECOVERY_PLANNED
        POLICY_EVALUATING      POLICY_EVALUATING
        APPROVAL_REQUIRED      APPROVAL_REQUIRED
        APPROVED               APPROVED                 (new)
        EXECUTING              EXECUTING
        VERIFYING              VERIFYING
        RECONCILING            RECONCILING              (new)
        MEASURING              MEASURING                (new)
        RESOLVED               RESOLVED

    The three `-ING` renames are not made. ADR-0016 settled that renaming for
    its own sake is "a large diff whose only effect is to change strings", and
    these names appear in 187 scenario expectations, the API contract and the
    stored rows of every incident ever raised.
    """
    DETECTED = "DETECTED"
    TRIAGED = "TRIAGED"
    INVESTIGATING = "INVESTIGATING"
    # v2 §20. The agent is running tools. Distinct from INVESTIGATING, which
    # only says a task was dispatched: this says evidence is actually arriving,
    # which is the difference between a run that started and one that is working.
    EVIDENCE_COLLECTING = "EVIDENCE_COLLECTING"
    # v2 §20. Weighing what the evidence means. ROOT_CAUSE_IDENTIFIED is the
    # RESULT; this is the activity, and §30's competing hypotheses are decided
    # here. Keeping both is what lets an incident that diagnosed and concluded
    # nothing be told apart from one that never got that far.
    DIAGNOSING = "DIAGNOSING"
    ROOT_CAUSE_IDENTIFIED = "ROOT_CAUSE_IDENTIFIED"
    RECOVERY_PLANNED = "RECOVERY_PLANNED"
    POLICY_EVALUATING = "POLICY_EVALUATING"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    # v2 §20. A human said yes and nothing has been executed yet. Previously
    # invisible: an incident went straight from APPROVAL_REQUIRED to EXECUTING,
    # so an approval granted against a provider that was down looked identical
    # to one nobody had answered.
    APPROVED = "APPROVED"
    EXECUTING = "EXECUTING"
    VERIFYING = "VERIFYING"
    # v2 §20. Verification could not establish external state and the
    # reconciliation sweep owns it now. Distinct from UNKNOWN: this says
    # somebody is looking, UNKNOWN says nobody could tell.
    RECONCILING = "RECONCILING"
    # v2 §20. Actions have settled and the ledger is totalling what was actually
    # recovered (§49). The gap between "we finished acting" and "we know what it
    # was worth" is real, and RESOLVED claimed both.
    MEASURING = "MEASURING"
    RESOLVED = "RESOLVED"
    # exception / terminal (MerchantOps §13, v2 §20)
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    ESCALATED = "ESCALATED"
    CANCELLED = "CANCELLED"
    CLOSED = "CLOSED"


# --------------------------------------------------------------------------
# Business entities
# --------------------------------------------------------------------------
class Tenant(Base):
    """MerchantOps §11, §54 — the outer isolation boundary.

    A tenant owns one or more merchants. Everything before this modelled
    merchant as if it were the top of the tree, which is correct for one
    merchant per tenant and has no way to express two: a business with a retail
    and a wholesale entity would need two unrelated logins, and a support user
    covering both could not exist.

    It is also a second boundary rather than a replacement. Merchant isolation
    still does the work on every request; tenant isolation is the check that
    holds if merchant isolation is ever wrong.
    """
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    policy_config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Both, deliberately. A user belongs to a tenant and is authorised for ONE
    # merchant within it — being in the right tenant is not authority over
    # every merchant the tenant owns.
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
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
# Provider-side objects for the non-refund actions (MerchantOps §18)
# --------------------------------------------------------------------------
class PaymentLink(Base):
    """A request for payment sent to a customer.

    This is the mock provider's state, the same role `refunds` plays for
    refunds: verification re-reads it rather than trusting what the create call
    returned. With live credentials Razorpay holds this and the table is only a
    local mirror.
    """
    __tablename__ = "payment_links"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    # The failed payment this link is trying to recover, for traceability.
    source_payment_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    status: Mapped[str] = mapped_column(String(32), default="created")  # created|paid|expired|cancelled
    short_url: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Notification(Base):
    """A message sent to a customer. Irreversible: it cannot be unsent."""
    __tablename__ = "notifications"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(ForeignKey("merchants.id"), index=True)
    customer_id: Mapped[str] = mapped_column(String(64), index=True)
    channel: Mapped[str] = mapped_column(String(16))            # email | sms
    template: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued|sent|failed
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
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
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

    # MerchantOps v2 §33. The band the PLATFORM computed from the evidence,
    # and the inputs it computed it from. Distinct from
    # `agent_tasks.agent_confidence`, which is the number the model chose for
    # itself: that one is an input here, capable of lowering this band and
    # never of raising it. See app/agent/confidence.py and ADR-0034.
    #
    # Nullable because an incident that has not been investigated has no
    # assessed confidence, and defaulting it to a value would be asserting one.
    confidence_band: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence_inputs: Mapped[dict] = mapped_column(JSON, default=dict)

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

    # --- §27 / v2 §38: the bounds ---
    max_recovery_minor: Mapped[int] = mapped_column(Integer)
    max_actions: Mapped[int] = mapped_column(Integer)
    max_attempts_per_customer: Mapped[int] = mapped_column(Integer)
    max_duration_seconds: Mapped[int] = mapped_column(Integer)

    # v2 §38 lists five bounds and this was the fifth: "Maximum risk". It was
    # enforced, but as a module constant shared by every campaign, which is a
    # different thing from a bound the campaign carries. §38's sentence is
    # "Every campaign must have explicit limits", and a limit nobody can see on
    # the campaign is not explicit -- an approver reading a plan could not tell
    # what risk it was authorised to take without reading the source.
    #
    # Copied at creation for the same reason the other four are: the bounds are
    # part of the decision that authorised the campaign, and lowering the global
    # ceiling must not silently retighten a campaign already in flight, nor
    # raising it silently widen one.
    max_risk_level: Mapped[str] = mapped_column(String(16), default="HIGH")

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

    # The share of this candidate's amount attributable to the incident. The
    # ledger measures at-risk, recoverable and attempted in this unit so the
    # figures nest (MerchantOps §49); `amount_minor` is the gross charge.
    attributed_amount_minor: Mapped[int] = mapped_column(Integer, default=0)
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
    # MerchantOps §41 — everything needed to reproduce and investigate a run.
    agent_version: Mapped[str] = mapped_column(String(64))
    model_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model_version: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(64))
    tool_registry_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    workflow_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scenario_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The incident this task investigates, when it was dispatched by one
    # (MerchantOps §13). Null for a task a user started by asking a question --
    # both entry points are legitimate and the loop is the same after this point.
    incident_id: Mapped[str | None] = mapped_column(
        ForeignKey("incidents.id"), nullable=True, index=True)
    is_replay: Mapped[bool] = mapped_column(Boolean, default=False)
    replayed_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # MerchantOps §37/§38. Agent state, stored beside the financial record and
    # never mixed into it. Neither gates anything: confidence is consulted by
    # nothing, and model_requires_human may only ADD to what policy already
    # decided (app/agent/output.py).
    agent_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_requires_human: Mapped[bool] = mapped_column(Boolean, default=False)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    llm_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    tool_calls: Mapped[list["ToolCall"]] = relationship(back_populates="task", order_by="ToolCall.seq")


class AgentMessage(Base):
    """The conversation the model actually saw — MerchantOps §66, §38.

    Tool calls and audit events recorded what the application DID. Nothing
    recorded what the model was looking at when it decided to do it, so
    "why did it call that tool" could only ever be reconstructed from the
    outside. A trace of effects is not a trace of reasoning.

    Stored per message as it is appended, not as a snapshot of the whole list
    each turn: the list accumulates, so snapshotting it would store the first
    message once per turn and the transcript would grow with the square of the
    conversation.

    This is agent state (§38). It sits beside the financial record and is never
    mixed into it: nothing here is evidence of anything having happened, only of
    something having been said.
    """
    __tablename__ = "agent_messages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    turn: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))          # user | assistant
    content: Mapped[list] = mapped_column(JSON, default=list)

    # Whether this message carries merchant or customer free text. A viewer that
    # renders a stored transcript needs to know which parts were quarantined
    # when the model saw them (§39) — stripping that distinction on the way into
    # storage would push the judgement onto every later reader.
    contains_untrusted: Mapped[bool] = mapped_column(Boolean, default=False)
    # A cheap proxy for how much context this message occupied. Not tokens: this
    # build has no tokeniser on the deterministic path, and a fabricated token
    # count is worse than an honest character count.
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_message_task_seq"),)


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
    # MerchantOps §59 names both. Both were already being spent and neither was
    # being recorded, which is the cheapest kind of missing metric.
    provider_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    verification_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    # MerchantOps §47 names it on every event. As a payload key it could not be
    # joined on or indexed, which is the one thing a correlation id is for.
    correlation_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
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


# --------------------------------------------------------------------------
# Event spine (MerchantOps v2 §11, §12, §13)
# --------------------------------------------------------------------------
class OutboxStatus(str, enum.Enum):
    PENDING = "PENDING"
    PUBLISHED = "PUBLISHED"
    # A consumer refused the event and retrying will not help — an unknown
    # event type, a payload that fails its own schema. Kept, not deleted:
    # v2 §12 exists so that a failure to publish is visible rather than silent.
    DEAD = "DEAD"


class EventOutbox(Base):
    """The transactional outbox — MerchantOps v2 §12.

    The problem this solves is stated in v2 §12 as a two-line failure:
    "Database update = success / Event publishing = failure". Anything that
    emits an event by calling a bus after committing has that bug, because the
    process can die in between. So the event is *written into the same
    transaction as the business state it describes* and published afterwards by
    a separate drain. Either both the incident and its `incident.created` row
    exist, or neither does.

    That inverts the failure mode rather than removing it: publishing can now
    happen twice (drain, crash, drain again) but can never be lost. Consumers
    must therefore be idempotent, which is why `id` is the event id a consumer
    deduplicates on rather than a surrogate.

    Field list is v2 §11's, verbatim. `entity_id` and `provider` are nullable
    because an internally-generated event has no provider and may name no
    single entity; forcing a value would mean inventing one.
    """
    __tablename__ = "event_outbox"

    # The event id v2 §11 requires. Also the deduplication key for consumers.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    event_type: Mapped[str] = mapped_column(String(64), index=True)
    schema_version: Mapped[str] = mapped_column(String(16), default="v1")

    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    merchant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)

    incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    payload_hash: Mapped[str] = mapped_column(String(64))

    status: Mapped[OutboxStatus] = mapped_column(
        Enum(OutboxStatus, native_enum=False), default=OutboxStatus.PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `occurred_at` is when the thing happened; `published_at` is when we told
    # anyone. They differ by however long the drain took, which is the latency
    # v2 §80 asks to be measured.
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now(), index=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    # The drain reads PENDING in occurrence order; this is the index it uses.
    __table_args__ = (Index("ix_outbox_drain", "status", "occurred_at"),)


# --------------------------------------------------------------------------
# Evidence graph (MerchantOps v2 §32)
# --------------------------------------------------------------------------
class Predicate(str, enum.Enum):
    """The relationships v2 §32 draws, and one it needs but does not name.

    §32's own figure uses four:

        Incident ── caused_by ──────> a cause
                 ── affects ────────> customers, payment attempts
                 ── creates ────────> revenue risk
                 └─ supported_by ───> E101 .. E104

    CONTRADICTS is the fifth. §32 has no use for it on its own, but §30's
    hypothesis engine rejects a hypothesis by weighing evidence against it, and
    §33's "evidence agreement" is not a computable quantity if disagreement
    cannot be expressed. A graph that can only record support can only ever
    agree with itself.
    """
    CAUSED_BY = "CAUSED_BY"
    AFFECTS = "AFFECTS"
    CREATES = "CREATES"
    SUPPORTED_BY = "SUPPORTED_BY"
    CONTRADICTS = "CONTRADICTS"


class EvidenceEdge(Base):
    """One typed relationship in an incident's evidence graph — v2 §32.

    §32's stated purpose is a question a merchant can ask:

        "Why do you believe this?"

    A flat list of evidence rows cannot answer it. It says what was looked at
    and not what any of it was taken to mean, so the reasoning stays in prose
    the platform did not write and cannot check.

    ## The graph is the platform's, not the model's

    Edges are written by deterministic code from state that already exists --
    detection signals, evidence rows, recovery candidates, the calculation
    engine's figures. The model may *cite* evidence, and `app/agent/output.py`
    already refuses a claim citing evidence that does not exist. It may not
    assert a relationship. An `AFFECTS` edge saying 1,842 customers were hit is
    a number, and §22 and §34 own numbers.

    ## It is a projection

    Every edge is derivable from the rows it points at, so the graph can be
    dropped and rebuilt. It is stored rather than computed on read because §32
    wants it queryable and because an edge records *when* the system drew the
    relationship, which a recomputation cannot recover.

    `object_value` carries the objects that are not rows -- a revenue figure, a
    count, a method name. Nullable `object_id` and nullable `object_value`
    rather than one polymorphic column: an edge to a row and an edge to a
    quantity are different things, and collapsing them would mean every reader
    guessing which it has.
    """
    __tablename__ = "evidence_edges"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Tenancy is checked outermost-first everywhere else (ADR-0025) and the
    # graph is no exception -- it is a read surface like any other.
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)

    # Every edge belongs to one incident's case. The graph is per-incident
    # because "why do you believe this?" is always asked about something.
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)

    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(64))
    predicate: Mapped[Predicate] = mapped_column(
        Enum(Predicate, native_enum=False), index=True)
    object_type: Mapped[str] = mapped_column(String(32))
    object_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_value: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Which deterministic producer drew it: a detection rule name, "recovery
    # planner", "calculation engine". Never a model. An edge whose origin
    # cannot be named is an assertion nobody owns.
    drawn_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True)

    # An incident asserting the same relationship twice is the same assertion,
    # so the graph refuses it rather than accumulating duplicates on every
    # re-run. This is the same reasoning as `incidents.detection_key`.
    #
    # NULLS NOT DISTINCT is load-bearing, not decoration. `object_id` is NULL
    # for every edge whose object is a quantity rather than a row -- the
    # revenue figure, the affected counts, the model's root cause -- and under
    # the SQL default two NULLs are distinct, so precisely those edges would
    # have escaped the constraint and duplicated on every re-investigation.
    # The edges most likely to be redrawn are the ones it would have missed.
    __table_args__ = (
        UniqueConstraint("incident_id", "subject_type", "subject_id", "predicate",
                         "object_type", "object_id", name="uq_edge_once",
                         postgresql_nulls_not_distinct=True),
        Index("ix_edge_subject", "incident_id", "subject_type", "subject_id"),
    )


# --------------------------------------------------------------------------
# Hypotheses (MerchantOps v2 §30)
# --------------------------------------------------------------------------
class HypothesisStatus(str, enum.Enum):
    """Where a candidate explanation stands once evidence has been weighed.

    UNTESTED is not a synonym for REJECTED and the distinction is the point.
    "We looked and found nothing supporting this" and "we have no way to look"
    lead to different next actions -- the second is a gap in the platform's
    instrumentation, and collapsing it into rejection would hide that gap by
    making it look like a settled question. Same reasoning as UNKNOWN against
    FAILED (§53) and INSUFFICIENT against LOW (§33).
    """
    SUPPORTED = "SUPPORTED"          # the strongest explanation the evidence allows
    CONTENDING = "CONTENDING"        # supported, but not more than another
    REJECTED = "REJECTED"            # evidence positively argues against it
    UNTESTED = "UNTESTED"            # nothing here can speak to it either way


class Hypothesis(Base):
    """One candidate explanation for an incident — MerchantOps v2 §30.

    §30's argument is that a single-shot answer is less robust than competing
    explanations tested against evidence:

        H1  UPI provider degradation
        H2  Merchant configuration problem
        H3  Traffic anomaly
        H4  Customer-segment-specific problem

    ## The platform adjudicates

    Hypotheses may be *proposed* by anyone -- a template set per incident type,
    or the model. Which one wins is decided by `app.evidence.hypotheses` from
    supporting and contradicting evidence, never by the model asserting it.
    This is §33's rule applied to explanations rather than to confidence: the
    model reasons, the platform decides what the reasoning established.

    ## Support lives in the graph, not in a column here

    `support_count` and `contradiction_count` are cached totals of
    `evidence_edges` rows whose subject is this hypothesis. The edges are the
    record; these are for ordering without a join. A count that disagrees with
    the edges is a bug in the writer, and `app.evidence.hypotheses.adjudicate`
    recomputes rather than incrementing so the two cannot drift apart.
    """
    __tablename__ = "hypotheses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    merchant_id: Mapped[str] = mapped_column(String(64), index=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), index=True)

    # "H1".."H4" — §30's own labels, stable within an incident so a merchant
    # reading the console and a reviewer reading the trace mean the same thing.
    label: Mapped[str] = mapped_column(String(8))
    # A short machine key ("provider_degradation"), so a scenario or a metric
    # can name a hypothesis without matching on prose.
    key: Mapped[str] = mapped_column(String(64), index=True)
    statement: Mapped[str] = mapped_column(Text)

    status: Mapped[HypothesisStatus] = mapped_column(
        Enum(HypothesisStatus, native_enum=False),
        default=HypothesisStatus.UNTESTED, index=True)

    # "hypothesis_engine" for the template set, "agent" for one the model added.
    proposed_by: Mapped[str] = mapped_column(String(32), default="hypothesis_engine")

    support_count: Mapped[int] = mapped_column(Integer, default=0)
    contradiction_count: Mapped[int] = mapped_column(Integer, default=0)
    # Why it ended where it did, in the platform's words rather than the
    # model's. A rejected hypothesis with no stated reason is an assertion.
    verdict_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    adjudicated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    # One hypothesis per key per incident. Re-investigating re-tests the same
    # four; it does not accumulate a fifth copy of each.
    __table_args__ = (
        UniqueConstraint("incident_id", "key", name="uq_hypothesis_once"),
    )

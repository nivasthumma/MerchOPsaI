"""Audit trail — CONTRACT §27, §39. Append-only from the application's view."""
from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.models import AuditLog

# MerchantOps §47/§58. Set for the duration of a run so every event it writes
# carries the same id without every call site having to pass one.
#
# A ContextVar rather than a module global, because the premise a global rests
# on is false here: FastAPI runs every `def` endpoint in a threadpool, so two
# tasks genuinely execute at once and a global would have them overwrite each
# other's id — tying together traces that have nothing to do with each other,
# which is the one thing a correlation id must never do. Starlette copies the
# request's context into the worker thread, so each run reads and writes its
# own value under both threads and async.
_CURRENT_CORRELATION: ContextVar[str | None] = ContextVar(
    "merchantops_correlation_id", default=None)


def set_correlation_id(value: str | None) -> None:
    _CURRENT_CORRELATION.set(value)


def current_correlation_id() -> str | None:
    return _CURRENT_CORRELATION.get()


@contextmanager
def correlation_scope(value: str) -> Iterator[str]:
    """Hold a correlation id for a block, then put back whatever was there.

    Restoring rather than clearing, because these nest. The request middleware
    sets one at the HTTP boundary and an agent run sets its own inside it; a run
    that finished by clearing the value to None would leave the rest of the
    request — the response, its status, its duration — logged as belonging to no
    trace at all, which is worse than the leak that clearing was meant to avoid.
    """
    token = _CURRENT_CORRELATION.set(value)
    try:
        yield value
    finally:
        _CURRENT_CORRELATION.reset(token)


_SECRET_KEYS = re.compile(r"(secret|password|api_key|token|authorization|key_secret)", re.I)
_SECRET_VALUE = re.compile(r"\b(rzp_(test|live)_[A-Za-z0-9]+|sk-[A-Za-z0-9\-_]{16,})\b")


def redact(value):
    """CONTRACT §37 — secrets never reach logs or traces."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if _SECRET_KEYS.search(str(k)) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


def record(session, task, event_type: str, payload: dict | None = None) -> AuditLog:
    entry = AuditLog(
        task_id=getattr(task, "id", None),
        # Carried when the task was dispatched by an incident, so the incident's
        # trail and the task's trail are the same trail (MerchantOps §47, §58).
        incident_id=getattr(task, "incident_id", None),
        merchant_id=getattr(task, "merchant_id", None),
        user_id=getattr(task, "user_id", None),
        event_type=event_type,
        correlation_id=_CURRENT_CORRELATION.get(),
        payload=redact(payload or {}),
    )
    session.add(entry)
    session.flush()
    return entry


def record_incident(session, incident, event_type: str,
                    payload: dict | None = None) -> AuditLog:
    """Audit an event that has an incident but no task.

    Detection and lifecycle transitions run with no task in scope. Without this
    they would be unauditable, and MerchantOps §47 requires the audit record to
    describe what the application actually did -- which includes the moves it
    made on its own.
    """
    entry = AuditLog(
        task_id=None,
        incident_id=getattr(incident, "id", None),
        merchant_id=getattr(incident, "merchant_id", None),
        user_id=None,
        event_type=event_type,
        # An incident's own id is its correlation id when nothing else is set,
        # so detection and lifecycle events join the same trace as the tasks
        # they dispatch.
        correlation_id=_CURRENT_CORRELATION.get() or getattr(incident, "correlation_id", None),
        payload=redact(payload or {}),
    )
    session.add(entry)
    session.flush()
    return entry


# MerchantOps §47 names its events in its own vocabulary. Ours are snake_case,
# they appear in scenario expectations and in stored rows, and renaming them
# would be a large diff whose only effect is to change strings — the same
# argument ADR-0016 made about section numbers. So they are published alongside
# instead, and a reader working from §47 can find the event it means.
#
# Events with no §47 name keep their own: the spec's list is explicitly
# "Examples", not an enumeration, and inventing a canonical name for something
# it never mentions would be pretending to a correspondence that is not there.
CANONICAL_EVENT: dict[str, str] = {
    "task_created": "TaskCreated",
    "task_completed": "TaskCompleted",
    "incident_detected": "IncidentCreated",
    "incident_investigated": "InvestigationCompleted",
    "incident_status_changed": "IncidentStateChanged",
    "tool_call": "EvidenceCollected",
    "agent_output": "RecommendationCreated",
    "policy_decision": "PolicyEvaluated",
    "policy_recheck": "PolicyEvaluated",
    "approval_requested": "ApprovalRequested",
    "approval_granted": "ApprovalGranted",
    "approval_rejected": "ApprovalRejected",
    "action_executing": "ActionStarted",
    "action_recorded": "ProviderResponseReceived",
    "verification": "VerificationCompleted",
    "reverification": "VerificationCompleted",
    "reconciliation_attempt": "ReconciliationAttempted",
    "recovery_planned": "RecoveryPlanned",
    "recovery_dispatched": "ActionStarted",
    "recovery_stopped": "RecoveryStopped",
}


def canonical(event_type: str) -> str:
    """§47's name for one of our events, or ours when it has none."""
    return CANONICAL_EVENT.get(event_type, event_type)


def _view(r: AuditLog) -> dict:
    return {"id": r.id, "at": r.created_at.isoformat(), "event": r.event_type,
            "canonical_event": canonical(r.event_type),
            "correlation_id": r.correlation_id, "task_id": r.task_id,
            "incident_id": r.incident_id, "payload": r.payload}


def trace_by_correlation(session, correlation_id: str,
                         merchant_id: str | None = None) -> list[dict]:
    """MerchantOps §58's complete trace: everything one operation touched.

    Detection, the incident's lifecycle, the tasks it dispatched, their tool
    calls, the policy decisions, the approval, the provider call, verification
    and reconciliation — in one ordering, because they are one story.
    """
    q = session.query(AuditLog).filter(AuditLog.correlation_id == correlation_id)
    if merchant_id is not None:
        # Merchant isolation applies to a trace exactly as it applies to a task.
        q = q.filter(AuditLog.merchant_id == merchant_id)
    return [_view(r) for r in q.order_by(AuditLog.id).all()]


def trace_for(session, task_id: str) -> list[dict]:
    rows = session.query(AuditLog).filter(AuditLog.task_id == task_id) \
        .order_by(AuditLog.id).all()
    return [_view(r) for r in rows]


def trace_for_incident(session, incident_id: str) -> list[dict]:
    """The incident-rooted trace of MerchantOps §58: detection, every lifecycle
    move, and every event of every task the incident dispatched -- in one
    ordering, because they are one story."""
    rows = session.query(AuditLog).filter(AuditLog.incident_id == incident_id) \
        .order_by(AuditLog.id).all()
    return [_view(r) for r in rows]

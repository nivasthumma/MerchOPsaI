"""Audit trail — CONTRACT §27, §39. Append-only from the application's view."""
from __future__ import annotations

import re

from app.models import AuditLog

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
        merchant_id=getattr(task, "merchant_id", None),
        user_id=getattr(task, "user_id", None),
        event_type=event_type,
        payload=redact(payload or {}),
    )
    session.add(entry)
    session.flush()
    return entry


def trace_for(session, task_id: str) -> list[dict]:
    rows = session.query(AuditLog).filter(AuditLog.task_id == task_id) \
        .order_by(AuditLog.id).all()
    return [{"id": r.id, "at": r.created_at.isoformat(), "event": r.event_type,
             "payload": r.payload} for r in rows]

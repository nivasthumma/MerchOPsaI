"""EventPublisher, EventConsumer, EventStore — MerchantOps v2 §13.

The transport is a PostgreSQL table (`event_outbox`) drained in occurrence
order. v2 §13 asks for the abstraction, not the infrastructure:

    The interface should remain: EventPublisher / EventConsumer / EventStore
    This allows future migration to Kafka, Redpanda, Cloud Pub/Sub, managed
    queues, without changing domain logic.

Two properties are what make the table a real outbox rather than a log:

**The insert shares the caller's session.** `publish` never opens a
transaction, never commits, and never flushes to a different connection. The
event becomes durable at exactly the moment the business state it describes
does. This is the whole point of v2 §12 and it is one line of discipline that
is easy to lose — a `with session_scope()` inside `publish` would silently
reintroduce the bug the outbox exists to fix.

**Delivery is at-least-once, in order, per merchant.** The drain claims rows
with `FOR UPDATE SKIP LOCKED` so two drains cannot deliver the same row, and
orders by `occurred_at` so a merchant's timeline never arrives shuffled. A
crash between "consumer ran" and "row marked PUBLISHED" redelivers. Consumers
must be idempotent; `Event.id` is the key to be idempotent on.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from sqlalchemy import select

from app.events.vocabulary import is_known
from app.models import EventOutbox, OutboxStatus


class UnknownEventType(ValueError):
    """Raised by `publish` for a type outside v2 §62's closed set."""


@dataclass(frozen=True)
class Event:
    """One event, as a consumer sees it. MerchantOps v2 §11's field list.

    Frozen because a consumer that mutates the event it was handed changes what
    the *next* consumer sees, and the resulting bug is invisible at both call
    sites.
    """
    id: str
    event_type: str
    schema_version: str
    tenant_id: str | None
    merchant_id: str | None
    entity_id: str | None
    provider: str | None
    incident_id: str | None
    task_id: str | None
    correlation_id: str | None
    occurred_at: datetime
    payload_hash: str
    payload: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """The wire shape. Used by the SSE endpoint and by tests."""
        return {
            "id": self.id,
            "event": self.event_type,
            "schema_version": self.schema_version,
            "tenant_id": self.tenant_id,
            "merchant_id": self.merchant_id,
            "entity_id": self.entity_id,
            "provider": self.provider,
            "incident_id": self.incident_id,
            "task_id": self.task_id,
            "correlation_id": self.correlation_id,
            "occurred_at": self.occurred_at.isoformat(),
            "payload_hash": self.payload_hash,
            "payload": self.payload,
        }


@runtime_checkable
class EventPublisher(Protocol):
    """v2 §13. Records that something happened, inside the caller's transaction."""

    def publish(self, session, event_type: str, **fields) -> Event: ...


@runtime_checkable
class EventConsumer(Protocol):
    """v2 §13. Reacts to an event. Must be idempotent on `event.id`."""

    def __call__(self, session, event: Event) -> None: ...


@runtime_checkable
class EventStore(Protocol):
    """v2 §13. What has happened, queryable."""

    def since(self, session, *, after: str | None, merchant_id: str | None,
              limit: int) -> list[Event]: ...

    def pending(self, session, *, limit: int) -> list[Event]: ...


# --------------------------------------------------------------------------
# Publishing
# --------------------------------------------------------------------------
def _hash(payload: dict) -> str:
    """v2 §11's `payload_hash`.

    `sort_keys` so the same content hashes the same regardless of how the dict
    was built; `default=str` so a datetime in a payload is a stable string
    rather than a TypeError at the one moment we are recording that something
    went wrong.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def publish(session, event_type: str, *, payload: dict | None = None,
            tenant_id: str | None = None, merchant_id: str | None = None,
            entity_id: str | None = None, provider: str | None = None,
            incident_id: str | None = None, task_id: str | None = None,
            correlation_id: str | None = None,
            schema_version: str = "v1") -> Event:
    """Write one event into the caller's transaction. MerchantOps v2 §12.

    Does not commit. The event is durable when the caller's transaction is, and
    is published to consumers later by `drain`. That ordering is the guarantee:
    a business write that rolls back takes its event with it, and a business
    write that commits cannot lose one.

    Raises `UnknownEventType` for a type outside v2 §62's set, at the call site,
    rather than emitting a frame no client subscribes to.
    """
    if not is_known(event_type):
        raise UnknownEventType(
            f"{event_type!r} is not one of MerchantOps v2 §62's events. "
            f"Add it to app.events.vocabulary.EVENT_TYPES if it should be."
        )

    body = payload or {}
    # Correlation defaults to whatever trace is in scope, so an event raised
    # deep inside a request joins that request's trace without every caller
    # having to thread the id through. Explicit always wins.
    if correlation_id is None:
        from app.audit.trace import current_correlation_id
        correlation_id = current_correlation_id()

    # Secrets and untrusted content are redacted on the same rules the audit
    # trail uses. An event stream is a wider audience than an audit table --
    # it reaches a browser -- so it cannot be the laxer of the two.
    from app.audit.trace import redact
    body = redact(body)

    row = EventOutbox(
        id=f"EVT_{uuid.uuid4().hex[:16].upper()}",
        event_type=event_type,
        schema_version=schema_version,
        tenant_id=tenant_id, merchant_id=merchant_id,
        entity_id=entity_id, provider=provider,
        incident_id=incident_id, task_id=task_id,
        correlation_id=correlation_id,
        payload=body, payload_hash=_hash(body),
        status=OutboxStatus.PENDING,
        occurred_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return _to_event(row)


def _to_event(row: EventOutbox) -> Event:
    return Event(
        id=row.id, event_type=row.event_type, schema_version=row.schema_version,
        tenant_id=row.tenant_id, merchant_id=row.merchant_id,
        entity_id=row.entity_id, provider=row.provider,
        incident_id=row.incident_id, task_id=row.task_id,
        correlation_id=row.correlation_id, occurred_at=row.occurred_at,
        payload_hash=row.payload_hash, payload=row.payload or {},
    )


# --------------------------------------------------------------------------
# Consuming
# --------------------------------------------------------------------------
_CONSUMERS: dict[str, list[Callable]] = {}


def subscribe(event_type: str, consumer: Callable) -> None:
    """Register a consumer for one event type.

    In-process, because there is one process. The registry is a module global
    for the same reason the event bus is a table: it is the smallest thing that
    satisfies the interface, and the interface is what makes it replaceable.
    """
    _CONSUMERS.setdefault(event_type, []).append(consumer)


def _consumers_for(event_type: str) -> list[Callable]:
    return _CONSUMERS.get(event_type, [])


def drain(session, *, limit: int = 200) -> dict:
    """Deliver pending events to their consumers, oldest first.

    Claims rows with `FOR UPDATE SKIP LOCKED`, so a second drain running
    concurrently takes different rows rather than delivering the same one
    twice. Returns counts rather than raising: a drain is a sweep, and one
    consumer that throws must not stop the events behind it.

    An event with no consumer is still marked PUBLISHED. It was published --
    to nobody, which is what having no subscribers means. The alternative is
    a table that grows forever because the UI has not been written yet.
    """
    rows = session.execute(
        select(EventOutbox)
        .where(EventOutbox.status == OutboxStatus.PENDING)
        .order_by(EventOutbox.occurred_at, EventOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ).scalars().all()

    published = failed = 0
    for row in rows:
        event = _to_event(row)
        try:
            for consumer in _consumers_for(row.event_type):
                consumer(session, event)
        except Exception as exc:  # one bad consumer must not block the queue
            row.attempts += 1
            row.last_error = f"{type(exc).__name__}: {exc}"[:500]
            # Three attempts, then DEAD. Kept rather than deleted so that a
            # consumer nobody noticed was broken is visible in the table
            # instead of only in a log that has rotated away.
            if row.attempts >= 3:
                row.status = OutboxStatus.DEAD
            failed += 1
            continue
        row.status = OutboxStatus.PUBLISHED
        row.published_at = datetime.now(UTC)
        published += 1

    session.flush()
    return {"claimed": len(rows), "published": published, "failed": failed}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------
class PostgresEventStore:
    """`EventStore` over `event_outbox`. MerchantOps v2 §13."""

    def since(self, session, *, after: str | None = None,
              merchant_id: str | None = None, limit: int = 100) -> list[Event]:
        """Events after a cursor, oldest first — the SSE resume path.

        The cursor is an event id rather than a timestamp because two events in
        the same transaction share a timestamp to the microsecond, and a
        timestamp cursor would either replay one or skip one. Ordering is
        `(occurred_at, id)` everywhere for the same reason.
        """
        q = select(EventOutbox)
        if merchant_id:
            q = q.where(EventOutbox.merchant_id == merchant_id)
        if after:
            anchor = session.get(EventOutbox, after)
            if anchor is not None:
                q = q.where(
                    (EventOutbox.occurred_at > anchor.occurred_at)
                    | ((EventOutbox.occurred_at == anchor.occurred_at)
                       & (EventOutbox.id > anchor.id))
                )
        rows = session.execute(
            q.order_by(EventOutbox.occurred_at, EventOutbox.id).limit(limit)
        ).scalars().all()
        return [_to_event(r) for r in rows]

    def pending(self, session, *, limit: int = 100) -> list[Event]:
        rows = session.execute(
            select(EventOutbox)
            .where(EventOutbox.status == OutboxStatus.PENDING)
            .order_by(EventOutbox.occurred_at, EventOutbox.id)
            .limit(limit)
        ).scalars().all()
        return [_to_event(r) for r in rows]

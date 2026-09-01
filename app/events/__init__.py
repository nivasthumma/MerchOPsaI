"""The event spine — MerchantOps v2 §11, §12, §13.

Three names and nothing else, because v2 §13 asks for exactly three:

    EventPublisher   something happened; record it durably
    EventConsumer    something happened; react to it
    EventStore       what has happened, queryable

The implementation behind them is a PostgreSQL table drained in order. v2 §13
is explicit that this is the right first implementation — "The first Razorpay
submission should not introduce Kafka simply for architectural appearance" —
and equally explicit that the *interface* is what keeps Kafka, Redpanda or a
managed queue reachable later without touching domain logic.

So the rule for callers is: import `publish`, `drain`, `EventStore`. Do not
import `app.models.EventOutbox`. The day the transport changes, everything that
went through these three names keeps working and everything that reached past
them does not.
"""
from app.events.bus import (
    EventConsumer,
    EventPublisher,
    EventStore,
    PostgresEventStore,
    drain,
    publish,
    subscribe,
)
from app.events.vocabulary import EVENT_TYPES, is_known

__all__ = [
    "EventPublisher", "EventConsumer", "EventStore", "PostgresEventStore",
    "publish", "drain", "subscribe", "EVENT_TYPES", "is_known",
]

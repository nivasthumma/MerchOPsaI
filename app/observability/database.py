"""Database timing — ADR-0031.

The operational question a log cannot otherwise answer: *which* query was slow.
A request line saying `duration_ms: 4200` tells you a request was slow and
nothing about where the time went, and in this application the plausible
answers are far apart — a model turn, a provider call, or a sweep over a
payments table that has grown since anyone last looked at it.

Attached with SQLAlchemy's event hooks rather than by wrapping the session,
because every query has to be counted, including the ones issued by code that
never sees a session object.

## Two things it deliberately does not do

**It does not log every statement.** At INFO that is one line per query and tens
of lines per request; the signal is drowned by the thing it is meant to find.
Only queries over `SLOW_QUERY_MS` are logged, and every query is counted.

**It does not label a metric with the SQL.** Statement text is unbounded, and a
label with unbounded values is a permanent series per distinct value. The metric
carries the operation (`SELECT`, `INSERT`, …); the slow-query log carries the
statement, truncated, for the cases worth reading.
"""
from __future__ import annotations

import os
import time

from sqlalchemy import event

from app.observability.logs import get_logger
from app.observability import runtime_metrics as metrics

log = get_logger("merchantops.db")

# Above this, a query is worth a line. Default chosen to be quiet on a healthy
# request and loud on the sweep that stopped being cheap.
SLOW_QUERY_MS = float(os.getenv("SLOW_QUERY_MS") or 250)

_installed = False


def _operation(statement: str) -> str:
    """The first word, uppercased and bounded. `SELECT`, `INSERT`, `CREATE`…

    Taken from the statement rather than the ORM because the ORM is not always
    the one issuing it, and bounded to a fixed set so a malformed statement
    cannot invent a new label value.
    """
    head = statement.lstrip()[:12].split(None, 1)[0].upper() if statement.strip() else "EMPTY"
    return head if head in {
        "SELECT", "INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER",
        "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "SET", "SHOW",
    } else "OTHER"


def install(engine) -> None:
    """Attach the timers. Idempotent; safe to call per engine construction."""
    global _installed
    if _installed:
        return
    _installed = True

    @event.listens_for(engine, "before_cursor_execute")
    def _before(conn, cursor, statement, parameters, context, executemany):
        conn.info.setdefault("_query_started", []).append(time.monotonic())

    @event.listens_for(engine, "after_cursor_execute")
    def _after(conn, cursor, statement, parameters, context, executemany):
        started = conn.info.get("_query_started")
        if not started:
            return
        elapsed = time.monotonic() - started.pop(-1)
        operation = _operation(statement)

        metrics.observe("merchantops_db_query_seconds", elapsed,
                        "Database query duration in seconds.",
                        {"operation": operation},
                        # Tighter than the HTTP buckets: a query that takes a
                        # second is already the problem, and 30s resolution
                        # would put every healthy query in the first bucket.
                        buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 1.0, 5.0))
        metrics.counter("merchantops_db_queries", "Database queries by operation.",
                        {"operation": operation})

        ms = elapsed * 1000
        if ms >= SLOW_QUERY_MS:
            # Never the parameters. They are merchant data and, in this schema,
            # sometimes the free text an injection attempt arrived in.
            log.warning("slow_query", extra={
                "operation": operation,
                "duration_ms": round(ms, 1),
                "statement": " ".join(statement.split())[:400],
            })


def reset_for_tests() -> None:
    global _installed
    _installed = False

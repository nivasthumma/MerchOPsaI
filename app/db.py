"""Database session management."""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    """The engine, built once per process.

    On a long-lived server that is exactly right: one pool, reused. On Vercel
    every instance is its own process, so a pool per instance multiplies into
    the database's connection limit — a cold-start spike can exhaust it while
    each individual instance looks healthy. There, hold no connections between
    invocations and let the platform's pooler do the pooling.
    """
    global _engine
    if _engine is None:
        serverless = bool(os.getenv("VERCEL"))
        _engine = create_engine(
            get_settings().database_url,
            future=True,
            pool_pre_ping=True,
            **({"poolclass": NullPool} if serverless else {}),
        )
        # Any process that opens a database connection is a process whose logs
        # matter -- the API, yes, but also the reconciliation sweep, the
        # evaluation runner and every script. Configuring only at the API
        # boundary left all of those emitting `logging`'s last-resort output:
        # bare text, no correlation id, straight to stderr. This is the one hook
        # they genuinely share.
        from app.observability.logs import configure_logging
        configure_logging()

        # Query timing and slow-query logging. Attached here rather than at an
        # API boundary for the same reason -- a sweep is where a query quietly
        # stops being cheap.
        from app.observability.database import install
        install(_engine)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


def checkpoint(session: Session) -> None:
    """Commit what has been recorded, before doing something irreversible.

    A request runs inside one transaction (`session_scope`), which is right for
    consistency and wrong for durability. An `agent_actions` row that has been
    flushed but not committed is undone by any later failure — an exception in
    the runtime, a lost connection, the platform killing the invocation
    mid-call. Undoing it after the provider has accepted the call leaves a
    refund at Razorpay with no record on our side: precisely the state the
    action record exists to make impossible.

    So the claim is committed before the provider is contacted. Everything
    committed alongside it is history that already happened — the task, its tool
    calls, the approval decision, the audit events. None of it is speculative,
    and none of it should be erased by what comes next.

    The session continues in a new transaction; `expire_on_commit=False` keeps
    every loaded object usable across the boundary. Under test the session
    factory joins the suite's outer transaction with `create_savepoint`, so this
    releases a SAVEPOINT and each test still rolls back to nothing.
    """
    session.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = get_session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

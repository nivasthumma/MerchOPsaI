"""The transaction-boundary invariant, as something that can be checked.

    No LLM, HTTP, provider, webhook-network or other external call occurs while
    a database transaction holding writes is open.

## Why

A write takes row locks and pins a connection. Held across a network call, the
lock lasts as long as the slowest provider response: a webhook for the same
action blocks behind it, a second worker waits on it, and a pool sized for
short transactions runs dry under a provider slowdown. The pattern the rest of
the code follows is

    short transaction -> COMMIT -> external call -> short transaction -> COMMIT

and `app.db.checkpoint` is the COMMIT.

## What is checked

Whether the session has written anything since its transaction last ended:

  * pending ORM changes not yet flushed (they will flush inside this
    transaction), or
  * a flush, or an INSERT / UPDATE / DELETE / `SELECT ... FOR UPDATE`
    statement, executed through the session since the root transaction began.

Reads are allowed -- a transaction that has only read holds no row locks. The
mark is cleared when the session's root transaction ends (commit or rollback);
releasing a SAVEPOINT does not clear it, because the outer transaction still
holds the writes.

This is tracked with session events rather than asked of PostgreSQL
(`pg_current_xact_id_if_assigned`), so it means the same thing in production
and under the test harness, where each test runs inside one outer transaction
and the database's own answer would be "yes" for every test that wrote a row.
That is what lets the whole suite run with the check in `strict` mode.

## Modes (`TRANSACTION_BOUNDARY_MODE`)

    warn    default: log a structured warning and record it
    strict  raise `BoundaryViolation` -- what the test suite runs under
    off     skip the check
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import event
from sqlalchemy.orm import Session

from app.config import get_settings

_log = logging.getLogger("merchantops.boundaries")

_WRITES = "merchantops_writes_open"

#: Violations seen in this process, newest last. Bounded.
VIOLATIONS: list[dict] = []
_MAX_KEPT = 200

_DML = re.compile(r"^\s*(insert|update|delete|merge)\b|\bfor\s+(no\s+key\s+)?update\b",
                  re.IGNORECASE)


class BoundaryViolation(BaseException):
    """An external call was attempted with a write transaction open.

    A BaseException, like KeyboardInterrupt, and deliberately so: provider
    reads sit inside `except Exception` blocks that turn any failure into
    UNKNOWN. A violation caught there would read as "the provider could not be
    asked", which is the one misreport this check exists to prevent. Only
    raised in `strict` mode.
    """


@event.listens_for(Session, "after_flush")
def _mark_flush(session, flush_context) -> None:
    session.info[_WRITES] = True


@event.listens_for(Session, "do_orm_execute")
def _mark_statement(state) -> None:
    if state.is_insert or state.is_update or state.is_delete:
        state.session.info[_WRITES] = True
        return
    stmt = state.statement
    if getattr(stmt, "_for_update_arg", None) is not None:
        state.session.info[_WRITES] = True
        return
    # `text()` statements: the raw SQL the mock adapter, the ledger and the
    # queue write with.
    raw = getattr(stmt, "text", None)
    if isinstance(raw, str) and _DML.search(raw):
        state.session.info[_WRITES] = True


@event.listens_for(Session, "after_transaction_end")
def _clear(session, transaction) -> None:
    if transaction.parent is None:
        session.info.pop(_WRITES, None)


def open_write(session) -> bool:
    """True if `session` holds writes in an open transaction."""
    if session is None:
        return False
    try:
        return bool(session.new or session.dirty or session.deleted
                    or session.info.get(_WRITES))
    except Exception:  # a check must never become the failure
        return False


def assert_no_open_write(session, what: str) -> None:
    """Called immediately before an external call. See the module docstring."""
    mode = get_settings().transaction_boundary_mode
    if mode == "off" or not open_write(session):
        return
    entry = {"call": what}
    VIOLATIONS.append(entry)
    del VIOLATIONS[:-_MAX_KEPT]
    if mode == "strict":
        raise BoundaryViolation(
            f"{what} attempted while a database transaction holding writes is "
            f"open. Commit (app.db.checkpoint) before calling out.")
    _log.warning("external call inside an open write transaction",
                 extra={"event": "transaction_boundary_violation", "call": what})

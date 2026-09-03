"""Structured logging — ADR-0031.

## Why this exists

There was no logging in this application. Not sparse logging: none — no
`logging` import anywhere under `app/`. The audit trail carried everything, and
it is excellent at what it is for, which is *domain* evidence: what the system
decided and why, kept forever, immutable.

It is no help at all operationally. It cannot tell you that a request 500'd,
that a query took four seconds, that the connection pool is exhausted, or what
the p99 is. Worse, the moments when you most need to know are exactly the ones
it cannot record: a run that dies before its transaction commits leaves nothing,
and a request that fails before reaching the runtime was never a task at all.

So: two channels, deliberately.

    audit_logs  ->  what the system decided        durable, immutable, per-tenant
    stdout      ->  what the process is doing      ephemeral, operational, ordered

They answer different questions and neither replaces the other. The bridge
between them is `correlation_id`, which is on both.

## Why JSON to stdout

Every platform this can run on ingests stdout — Vercel, CloudWatch, Datadog,
`journalctl`, a terminal. Writing anywhere else means choosing a vendor at the
logging layer, which is the wrong place to choose one. JSON because a log line
nobody can query is a log line nobody reads.

Set `LOG_FORMAT=text` for a human-readable local format.

## Redaction

Every payload goes through `app.audit.trace.redact`, the same function the audit
trail uses. One redaction rule, one place. A secret that must not reach the
database must not reach stdout either, and stdout is the easier of the two to
forward somewhere unexpected.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any

from app.audit.trace import current_correlation_id, redact

# Keys `logging` puts on every record. Anything else a caller passed via
# `extra=` is ours and belongs in the payload.
_RESERVED = frozenset((
    "args", "asctime", "created", "exc_info", "exc_text", "filename", "funcName",
    "levelname", "levelno", "lineno", "module", "msecs", "message", "msg", "name",
    "pathname", "process", "processName", "relativeCreated", "stack_info",
    "taskName", "thread", "threadName",
))


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the correlation id attached for free."""

    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S") + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }

        # Read here rather than at the call site so every line carries it
        # without anyone having to remember. This is the join key between a log
        # line and the audit trail row describing the same moment.
        correlation = current_correlation_id()
        if correlation:
            entry["correlation_id"] = correlation

        extras = {k: v for k, v in record.__dict__.items()
                  if k not in _RESERVED and not k.startswith("_")}
        if extras:
            entry.update(redact(extras))

        if record.exc_info:
            # The type and message, never the frames. A stack trace on stdout is
            # how a connection string ends up in a log aggregator, and the
            # trace is on the record for a local formatter that wants it.
            exc_type, exc_value, _ = record.exc_info
            entry["error"] = {
                "type": getattr(exc_type, "__name__", str(exc_type)),
                "message": redact(str(exc_value)),
            }

        # `default=str` so a datetime or an Enum in an extra logs as itself
        # rather than raising inside the logger, which would lose the line it
        # was trying to write.
        return json.dumps(entry, default=str, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """For a terminal. Same fields, laid out for eyes rather than queries."""

    def format(self, record: logging.LogRecord) -> str:
        correlation = current_correlation_id()
        head = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<5} {record.name}"
        if correlation:
            head += f" [{correlation}]"
        extras = {k: v for k, v in record.__dict__.items()
                  if k not in _RESERVED and not k.startswith("_")}
        tail = "  " + " ".join(f"{k}={v}" for k, v in redact(extras).items()) if extras else ""
        line = f"{head}  {record.getMessage()}{tail}"
        if record.exc_info:
            exc_type, exc_value, _ = record.exc_info
            line += f"  error={getattr(exc_type, '__name__', exc_type)}: {redact(str(exc_value))}"
        return line


_configured = False


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install the handler. Idempotent; safe to call from several entrypoints.

    Called from `app/api/main.py` at import, so anything that imports the API
    gets it — including the Vercel function, the scripts and the test suite. A
    logger that only works when someone remembers to configure it is a logger
    that is silent in exactly the deployment nobody set up by hand.
    """
    global _configured
    if _configured:
        return

    level = (level or os.getenv("LOG_LEVEL") or "INFO").upper()
    fmt = (fmt or os.getenv("LOG_FORMAT") or "json").lower()

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(TextFormatter() if fmt == "text" else JsonFormatter())

    root = logging.getLogger()
    # Replace rather than add. Uvicorn installs its own handlers, and leaving
    # them produces every line twice in one format and once in another.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

    # Uvicorn's access log duplicates our request middleware, in a format that
    # carries no correlation id. Ours is the one that can be joined to a trace.
    logging.getLogger("uvicorn.access").disabled = True

    # Libraries that log every outbound call at INFO. Useful when chasing a
    # provider problem, and otherwise one line per HTTP request on top of ours.
    # Raise with LOG_LEVEL_HTTP=INFO for a debugging session.
    http_level = (os.getenv("LOG_LEVEL_HTTP") or "WARNING").upper()
    for chatty in ("httpx", "httpx2", "httpcore", "urllib3", "anthropic"):
        logging.getLogger(chatty).setLevel(http_level)

    for keep in ("uvicorn.error", "sqlalchemy.engine", "alembic"):
        logging.getLogger(keep).propagate = True

    _configured = True


def reset_logging_for_tests() -> None:
    """Let a test reconfigure. Nothing else should call this."""
    global _configured
    _configured = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

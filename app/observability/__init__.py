"""Operational visibility — logs, metrics, request instrumentation.

Separate from `app/audit/`, which is the domain's evidence: durable, immutable,
per-tenant, and answering what the system *decided*. This package answers what
the process is *doing*, and is ephemeral by design. `correlation_id` is on both,
and is how you get from one to the other.
"""
from app.observability.logs import configure_logging, get_logger

__all__ = ["configure_logging", "get_logger"]

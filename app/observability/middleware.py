"""Request instrumentation — ADR-0031.

One place where every request gets a correlation id, a log line, and a metric.
Per-endpoint instrumentation would be sixty chances to forget one, and the
endpoints that would get forgotten are the ones nobody thinks about — which are
the ones that break.

    correlation id   accepted from the caller, or minted here
    log line         one per request, on the way out, with the outcome
    metrics          count by route+method+status, duration histogram

## The route template, not the path

`/tasks/TASK_9F2A31C0` is recorded as `/tasks/{task_id}`. A metric labelled with
the raw path grows one time series per task and never shrinks — a monitoring
system that runs the process out of memory. Starlette resolves the template for
us; when it cannot (a 404 on an unrouted path), the label is `<unmatched>`
rather than whatever the caller typed, because an attacker choosing URLs must
not be able to choose our label values either.

## Why the log line comes last

Logging on the way in doubles the volume and tells you nothing you cannot infer
from the line on the way out, except in the one case that matters: a request
that never finishes. That case is covered — an exception still produces the
line, and a request killed by the host produces none, which is itself the
signal.
"""
from __future__ import annotations

import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.audit.trace import correlation_scope
from app.observability import runtime_metrics as metrics
from app.observability.logs import get_logger

log = get_logger("merchantops.request")

# Accepted from the caller so a trace can span a gateway, a queue and this
# service. It is a label and never an authorisation input: a caller choosing its
# own id can confuse a search, not gain access.
CORRELATION_HEADERS = ("X-Correlation-ID", "X-Request-ID")

_MAX_ID = 64


def _incoming_id(request) -> str | None:
    for header in CORRELATION_HEADERS:
        value = request.headers.get(header)
        if value:
            # Bounded and stripped of anything that would break a log parser or
            # let a caller inject fields into a JSON line.
            cleaned = "".join(c for c in value[:_MAX_ID] if c.isalnum() or c in "-_")
            if cleaned:
                return cleaned
    return None


def _route_template(request) -> str:
    """`/tasks/{task_id}`, never `/tasks/TASK_9F2A31C0`."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "<unmatched>"


class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        correlation = _incoming_id(request) or f"COR_{uuid.uuid4().hex[:12].upper()}"
        started = time.monotonic()

        with correlation_scope(correlation):
            status = 500
            failed: BaseException | None = None
            try:
                response = await call_next(request)
                status = response.status_code
                # Handed back so a caller can quote it in a bug report, and so a
                # browser request can be joined to the trail it produced.
                response.headers["X-Correlation-ID"] = correlation
                return response
            except BaseException as exc:                      # noqa: BLE001
                # Re-raised untouched. This is here to make sure the failure is
                # counted and logged, not to handle it.
                failed = exc
                raise
            finally:
                duration = time.monotonic() - started
                # Resolved after the call: Starlette attaches the route during
                # dispatch, so reading it earlier gives <unmatched> for
                # everything.
                route = _route_template(request)
                labels = {"route": route, "method": request.method}

                metrics.observe("merchantops_http_request_seconds", duration,
                                "Request duration in seconds.", labels)
                metrics.counter("merchantops_http_requests",
                                "HTTP requests by route, method and status.",
                                {**labels, "status": str(status)})
                if status >= 500:
                    metrics.counter("merchantops_http_server_errors",
                                    "Responses this service is at fault for.", labels)

                fields = {
                    "method": request.method,
                    "route": route,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round(duration * 1000, 2),
                }
                if failed is not None:
                    log.error("http_request_failed", extra=fields, exc_info=failed)
                elif status >= 500:
                    log.error("http_request", extra=fields)
                elif status >= 400:
                    # A 401 or a 409 is the system working. It belongs in the
                    # log at a level that does not page anybody.
                    log.warning("http_request", extra=fields)
                else:
                    log.info("http_request", extra=fields)

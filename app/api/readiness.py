"""Liveness and readiness — MerchantOps §11 / plan §11.

`/health` answered one question — what is this process configured to do — and
was used to answer three:

  * is this process alive?              (a restarter asks this)
  * can it serve a request?             (a load balancer asks this)
  * what is the run configuration?      (a person asks this)

Those want different answers and, critically, different *failure modes*. A
liveness probe that touches the database restarts the whole API when the
database blips, which is the one action guaranteed not to help. A readiness
probe that never touches the database happily accepts traffic into a process
that cannot answer anything.

So:

    /liveness    the process is running and can execute code. No I/O. Never
                 fails for a dependency's reason.
    /readiness   every dependency this build actually has, checked, with a
                 per-component verdict and the overall verdict derived from
                 which of them are *required*.
    /health      unchanged — the run configuration, and still unauthenticated
                 so the posture is readable before anyone signs in.

## Degraded is a real state

A component can be down without the API being useless. Reconciliation being
behind does not stop an operator reading an incident; the LLM being unreachable
falls back to the deterministic planner, which is a documented mode and not an
outage. Collapsing those into `not ready` takes the API out of rotation for
problems taking it out of rotation cannot fix.

So each component reports `healthy | degraded | down | not_configured`, and
readiness is `ready` unless a **required** component is down. `not_configured`
is deliberately distinct from `down`: no webhook secret is a posture, and
reporting it as a failure teaches people to ignore the endpoint.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import get_settings

HEALTHY = "healthy"
DEGRADED = "degraded"
DOWN = "down"
NOT_CONFIGURED = "not_configured"

# Components whose failure means this process cannot serve. Deliberately short:
# everything else degrades. The database is the only thing on it, because it is
# the only dependency without which every route returns 500.
REQUIRED = frozenset({"database"})

# How far behind reconciliation may fall before it is worth saying so. Ten
# minutes is two of the recommended five-minute cron intervals: one missed run
# is a blip, two is a cron that is not running.
RECONCILIATION_LAG_SECONDS = 600


def _timed(fn) -> tuple[dict, float]:
    t0 = time.perf_counter()
    out = fn()
    return out, (time.perf_counter() - t0) * 1000.0


def _database(session) -> dict:
    """A real query, not a connection check.

    `SELECT 1` proves the socket is open. It does not prove the schema this
    build needs is there, which is the failure mode of a half-applied migration
    — and a process that passes readiness and then 500s on every route is worse
    than one that fails readiness honestly.
    """
    try:
        session.execute(text("SELECT 1")).scalar()
        # The tables every route depends on. Cheap: PostgreSQL answers this from
        # the catalogue without touching the tables themselves.
        missing = session.execute(text("""
            SELECT t.name FROM (VALUES
                ('payments'), ('agent_actions'), ('agent_tasks'), ('approvals'),
                ('incidents'), ('audit_logs'), ('webhook_events'),
                ('provider_mappings')
            ) AS t(name)
            WHERE to_regclass('public.' || t.name) IS NULL
        """)).scalars().all()
    except Exception as exc:
        return {"status": DOWN, "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}

    if missing:
        return {"status": DOWN,
                "detail": f"Schema is incomplete; missing {', '.join(sorted(missing))}. "
                          f"Run `make migrate`."}
    return {"status": HEALTHY, "detail": "Reachable, schema present."}


def _payment_provider(session) -> dict:
    """Which provider universe this process is in, and whether it answers.

    Not a call to Razorpay. A readiness probe that makes an outbound API call on
    every scrape is a readiness probe that gets the deployment rate-limited, and
    the useful fact here — mock or Test Mode, and which environment mappings
    resolve in — is local.
    """
    from app.integrations.mapping import active_environment, mapping_coverage

    s = get_settings()
    mode = s.resolved_razorpay_mode
    cov = mapping_coverage(session)
    # The mode and the environment are already on `/health`, which is public.
    # The coverage counts are not, so they stay in the structured fields that
    # `_public` strips rather than being baked into the detail string an
    # unauthenticated probe reads.
    detail = f"{mode}, environment {active_environment()}"
    if mode == "mock":
        # Not degraded. The mock is a documented mode with the same policy,
        # approval, idempotency and verification path — only the outbound call
        # differs — and marking it degraded would make "degraded" the normal
        # state, which is how a status light stops being read.
        return {"status": HEALTHY, "detail": f"Mock adapter. {detail}",
                "execution_is_real": False, **cov}
    return {"status": HEALTHY, "detail": f"Live Test Mode. {detail}",
            "execution_is_real": True, **cov}


def _llm() -> dict:
    s = get_settings()
    if s.resolved_llm_provider == "deterministic":
        return {"status": NOT_CONFIGURED,
                "detail": ("Deterministic planner. Reasoning measures the control "
                           "plane, not a model."),
                "provider": "deterministic", "model": "deterministic-planner-v1"}
    return {"status": HEALTHY,
            "detail": f"{s.llm_model} via {s.anthropic_credential_source}.",
            "provider": "anthropic", "model": s.llm_model}


def _webhook_ingestion(session) -> dict:
    s = get_settings()
    if not s.webhook_verification_enabled:
        return {"status": NOT_CONFIGURED,
                "detail": ("No RAZORPAY_WEBHOOK_SECRET. Deliveries are stored with "
                           "signature_valid=false and are never acted on."),
                "signature_verification": False}

    row = session.execute(text("""
        SELECT COUNT(*) FILTER (WHERE status = 'INVALID'
                                AND received_at > now() - interval '1 hour') AS invalid,
               COUNT(*) FILTER (WHERE status = 'RECEIVED'
                                AND received_at < now() - interval '5 minutes') AS stuck,
               MAX(received_at) AS last_received
          FROM webhook_events
    """)).mappings().one()

    if int(row["stuck"] or 0):
        # Received and never processed is the failure that looks like success:
        # the endpoint returned 200, the provider is satisfied, and nothing
        # reconciled.
        return {"status": DEGRADED,
                "detail": ("Deliveries are stored but never processed. The "
                           "endpoint returned 200 and nothing reconciled."),
                "stuck": int(row["stuck"]),
                "signature_verification": True,
                "last_received": row["last_received"].isoformat()
                                 if row["last_received"] else None}
    return {"status": HEALTHY,
            "detail": "Ingesting, with signature verification on.",
            "rejected_last_hour": int(row["invalid"] or 0),
            "signature_verification": True,
            "last_received": row["last_received"].isoformat()
                             if row["last_received"] else None}


def _reconciliation(session) -> dict:
    """The sweep is this build's worker. Whether it is running is a readiness
    fact, because an action that goes UNKNOWN while nothing is sweeping stays
    UNKNOWN until somebody notices by hand."""
    row = session.execute(text("""
        SELECT COUNT(*) FILTER (WHERE escalated = true)              AS escalated,
               COUNT(*) FILTER (WHERE escalated = false
                                AND verification_state IN ('UNKNOWN','PARTIAL'))
                                                                     AS unsettled,
               -- COALESCE, because a NULL schedule means "due now", not "never
               -- due". Taking MIN(next_verify_at) alone returns NULL over a
               -- population of unscheduled rows -- which is exactly what a
               -- backlog written before the schedule existed looks like -- and
               -- reported the stalest possible state as healthy.
               MIN(COALESCE(next_verify_at, updated_at))
                 FILTER (WHERE escalated = false
                         AND verification_state IN ('UNKNOWN','PARTIAL'))
                                                                     AS oldest_due
          FROM agent_actions
    """)).mappings().one()

    escalated = int(row["escalated"] or 0)
    unsettled = int(row["unsettled"] or 0)
    oldest_due = row["oldest_due"]

    overdue_by = None
    if oldest_due is not None:
        due = oldest_due if oldest_due.tzinfo else oldest_due.replace(tzinfo=UTC)
        overdue_by = (datetime.now(UTC) - due).total_seconds()

    status = HEALTHY
    # No counts in the public string, for the same reason as above. "Nothing
    # overdue" and "the sweep is behind" are both actionable without saying how
    # much money is involved.
    detail = "Nothing is overdue for re-verification."
    if overdue_by is not None and overdue_by > RECONCILIATION_LAG_SECONDS:
        status = DEGRADED
        detail = (f"An action has been due for re-verification for "
                  f"{int(overdue_by)}s. The sweep may not be running "
                  f"(`scripts/reconcile.py`).")
    return {"status": status, "detail": detail,
            "unsettled": unsettled, "escalated": escalated,
            "oldest_due_overdue_seconds": int(overdue_by) if overdue_by else None}


def _mapping_integrity(session) -> dict:
    """`payments.external_payment_id` and `provider_mappings` must agree.

    Two authorities on one fact, kept deliberately (see
    `app.integrations.mapping`) and therefore checked rather than trusted. A
    disagreement means an execution path and a mock-provider read could resolve
    the same internal payment differently, which is the exact defect the
    mapping table was introduced to make impossible.
    """
    from app.integrations.mapping import check_consistency

    try:
        drift = check_consistency(session)
    except Exception as exc:
        return {"status": DOWN, "detail": f"{type(exc).__name__}: {str(exc)[:160]}"}
    if drift:
        return {"status": DEGRADED,
                "detail": ("The mapping table and payments.external_payment_id "
                           "disagree. Execution resolves through the table; the "
                           "authenticated report names the rows."),
                "drifted": len(drift),
                # Payment ids. Authenticated callers only -- `_public` strips it.
                "sample": drift[:5]}
    return {"status": HEALTHY, "detail": "Mapping table and payment columns agree."}


def liveness() -> dict:
    """The process is running. No I/O, no dependencies, never fails for
    something a restart cannot fix."""
    return {"status": "alive", "checked_at": datetime.now(UTC).isoformat()}


# The keys every check may publish to an unauthenticated caller. A probe needs
# the verdict; it does not need the counts.
#
# This is the same line `/metrics/prometheus` already draws — that route refuses
# to serve without a scrape token because "route names, traffic shape and error
# rates" are a smaller leak than data and still a leak. Readiness carries the
# same shape of fact (how many payments exist, how far behind the sweep is, how
# many deliveries were rejected), so it draws the line in the same place rather
# than in a different one for no reason.
PUBLIC_KEYS = frozenset({"status", "detail", "required", "latency_ms"})


def _public(components: dict[str, dict]) -> dict[str, dict]:
    """The same verdicts, without the operational detail.

    `detail` is kept: it is what makes a red probe actionable, and it is written
    to be readable by whoever is holding the pager. Where a detail string would
    itself carry a count, the check composes it from figures that are already
    public elsewhere (`/health` publishes the adapter mode) or that describe
    this process rather than this merchant's data.
    """
    return {name: {k: v for k, v in c.items() if k in PUBLIC_KEYS}
            for name, c in components.items()}


def readiness(session, *, detailed: bool = False) -> dict:
    """Every dependency, checked, with the overall verdict derived.

    The verdict is computed from `REQUIRED` rather than written by each check,
    so a component cannot decide on its own that its failure should take the
    API out of rotation.

    `detailed` is granted only to an authenticated caller. Everyone else gets
    the verdicts, which is what a probe is for.
    """
    components: dict[str, dict] = {}
    for name, check in (
        ("database", lambda: _database(session)),
        ("payment_provider", lambda: _payment_provider(session)),
        ("llm", _llm),
        ("webhook_ingestion", lambda: _webhook_ingestion(session)),
        ("reconciliation", lambda: _reconciliation(session)),
        ("mapping_integrity", lambda: _mapping_integrity(session)),
    ):
        try:
            result, ms = _timed(check)
        except Exception as exc:  # a check must never take the endpoint down
            result, ms = ({"status": DOWN,
                           "detail": f"Check raised {type(exc).__name__}: "
                                     f"{str(exc)[:160]}"}, 0.0)
        components[name] = {**result, "required": name in REQUIRED,
                            "latency_ms": round(ms, 2)}

    blocking = [n for n, c in components.items()
                if c["required"] and c["status"] == DOWN]
    degraded = [n for n, c in components.items() if c["status"] == DEGRADED]

    if blocking:
        status = "not_ready"
    elif degraded:
        status = "degraded"
    else:
        status = "ready"

    return {
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "components": components if detailed else _public(components),
        # Named rather than left for the reader to derive from six verdicts.
        "blocking": blocking,
        "degraded": degraded,
    }

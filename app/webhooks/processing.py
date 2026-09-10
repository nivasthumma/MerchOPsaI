"""What a verified webhook actually causes — MerchantOps §34, §35.

The webhook decides *when* to look. It never decides *what was found*.

    verified event naming an entity
        -> find the actions that touch that entity
        -> re-read provider state through the adapter   <- the authority
        -> settle, or raise a mismatch incident
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.razorpay.adapter import get_adapter
from app.models import (
    AgentAction,
    IncidentSeverity,
    IncidentType,
    VerificationState,
    WebhookEvent,
    WebhookStatus,
)

DETECTION_VERSION = "reconciliation-v1"

# A regression away from SUCCESS is a contradiction: we told someone money moved
# and the provider now says otherwise. UNKNOWN is deliberately excluded -- that
# is a failure to read, not a disagreement about what is true, and raising an
# incident for it would turn every provider blip into a false alarm.
CONTRADICTS_SUCCESS = frozenset({VerificationState.FAILED, VerificationState.PARTIAL})


def _actions_for_entity(session, entity_id: str) -> list[AgentAction]:
    """Actions this event could be about. An event names either the payment or
    the refund, so both columns are candidates."""
    return (session.query(AgentAction)
            .filter((AgentAction.external_payment_id == entity_id)
                    | (AgentAction.external_reference == entity_id))
            .order_by(AgentAction.created_at)
            .all())


def _raise_mismatch(session, action: AgentAction, before: VerificationState,
                    after: VerificationState, event: WebhookEvent):
    """MerchantOps §35 — surfaced, never silently corrected.

    The internal record said the money moved. The provider, read back
    independently, says it did not. Overwriting our record and moving on would
    erase the only evidence that the two ever disagreed.
    """
    from app.incidents.manager import raise_incident

    return raise_incident(
        session,
        merchant_id=action.merchant_id,
        incident_type=IncidentType.RECONCILIATION_MISMATCH,
        # Always CRITICAL: a financial claim we already made is in doubt. There
        # is no small version of this.
        severity=IncidentSeverity.CRITICAL,
        title=f"Reconciliation mismatch on {action.target_payment_id}",
        summary=(
            f"Action {action.id} was recorded as {before.value} but provider state "
            f"now reads {after.value}. Internal state and provider state disagree "
            f"about a completed financial action; no correction has been applied."
        ),
        # One incident per (action, contradicted state), not one per redelivery.
        detection_key=f"{action.merchant_id}|RECONCILIATION_MISMATCH|{action.id}|{after.value}",
        detection_rule="provider_contradicts_internal_state",
        detection_version=DETECTION_VERSION,
        revenue_at_risk_minor=action.amount_minor,
        signals={
            # The §12 canonical four, same shape every other rule emits. The
            # baseline here is our own recorded state and the observation is
            # what the provider says now — which is the whole content of a
            # reconciliation mismatch.
            "baseline": before.value, "observed": after.value,
            "threshold": "provider state must not contradict a SUCCESS we recorded",
            "unit": "verification state",
            "action_id": action.id, "task_id": action.task_id,
            "target_payment_id": action.target_payment_id,
            "external_payment_id": action.external_payment_id,
            "external_reference": action.external_reference,
            "amount_minor": action.amount_minor,
            "internal_state_before": before.value,
            "provider_state_now": after.value,
            "webhook_event_id": event.event_id,
            "webhook_event_type": event.event_type,
        },
        evidence=[
            {"key": "internal_state_before", "value": before.value, "source": "agent_actions"},
            {"key": "provider_state_now", "value": after.value, "source": "razorpay"},
            {"key": "external_reference", "value": action.external_reference, "source": "razorpay"},
            {"key": "webhook_event", "value": event.event_id, "source": "webhook_events"},
        ],
    )


def _settle_candidates_plan(session, action, adapter) -> str | None:
    """Settle the plan the action's candidate belongs to."""
    from app.models import RecoveryCandidate, RecoveryPlan
    from app.recovery.dispatch import settle_plan

    cand = session.get(RecoveryCandidate, action.recovery_candidate_id)
    if cand is None:
        return None
    plan = session.get(RecoveryPlan, cand.plan_id)
    if plan is None:
        return None
    settle_plan(session, plan, adapter)
    return plan.id


def process_event(session, event: WebhookEvent, adapter=None):
    """Re-verify whatever this event touches. Returns an IngestResult."""
    from app.tools.actions import reverify_action
    from app.webhooks.razorpay import IngestResult

    adapter = adapter or get_adapter(session)
    reverified: list[str] = []
    incident_id: str | None = None
    notes: list[str] = []
    settled_plans: set[str | None] = set()

    actions = _actions_for_entity(session, event.entity_id) if event.entity_id else []
    if not actions:
        event.status = WebhookStatus.IGNORED
        event.processed_at = datetime.now(UTC)
        event.processing_note = (
            f"No action of ours touches {event.entity_id}. Recorded as provider "
            f"history; nothing to reconcile.")
        session.flush()
        return IngestResult(WebhookStatus.IGNORED, event.event_id, stored_id=event.id,
                            note=event.processing_note)

    for action in actions:
        before = action.verification_state
        try:
            vr = reverify_action(session, adapter, action)
        except Exception as exc:
            # A failed read is not a settlement, and it is certainly not a
            # reason to believe the payload. Leave the action as it was; the
            # reconciliation sweep will try again.
            notes.append(f"{action.id}: provider read failed ({type(exc).__name__})")
            continue

        reverified.append(action.id)

        if before is VerificationState.SUCCESS and vr.state in CONTRADICTS_SUCCESS:
            inc = _raise_mismatch(session, action, before, vr.state, event)
            if inc is not None:
                incident_id = inc.id
                notes.append(f"{action.id}: {before.value} -> {vr.state.value}, "
                             f"raised {inc.id}")
            else:
                notes.append(f"{action.id}: {before.value} -> {vr.state.value}, "
                             f"mismatch already raised")
        else:
            notes.append(f"{action.id}: {before.value if before else 'None'} "
                         f"-> {vr.state.value}")

        # §49. An action that came from a recovery candidate settles its plan
        # here, so a paid link is recorded as recovered when the provider says
        # so rather than when someone next asks.
        #
        # AFTER the mismatch, never before: `settle_plan` commits ahead of its
        # own provider reads, and a failure after that commit used to leave the
        # contradicted state durable with no incident -- and a retry then read
        # FAILED as "before" and never raised one. Committed together, or not
        # at all.
        if action.recovery_candidate_id:
            settled_plans.add(_settle_candidates_plan(session, action, adapter))

    event.status = WebhookStatus.PROCESSED
    event.processed_at = datetime.now(UTC)
    plans = sorted(p for p in settled_plans if p)
    if plans:
        notes.append(f"settled plan(s): {', '.join(plans)}")
    event.processing_note = "; ".join(notes) or "No action taken."
    session.flush()

    return IngestResult(WebhookStatus.PROCESSED, event.event_id, stored_id=event.id,
                        note=event.processing_note, reverified=reverified,
                        incident_id=incident_id)


# --------------------------------------------------------------------------
# Asynchronous processing — validate -> persist -> dedupe -> ACK -> HERE
# --------------------------------------------------------------------------
#: How long a claim is honoured before another worker may take the row over.
#: Also the pacing between attempts on a delivery whose processing failed.
#: Longer than one delivery can take to process: it may page a payment's
#: refunds and read back several actions and links, each read bounded at 10s.
#: A lease shorter than that hands a delivery to a second worker mid-run.
CLAIM_LEASE_SECONDS = 900
#: Attempts before a delivery is dead-lettered (status FAILED). Bounded: a
#: delivery that can never be processed must stop being retried and become
#: visible, not be retried forever.
MAX_ATTEMPTS = 5


def _claim_next(session, *, lease_seconds: int, max_attempts: int,
                exclude: list[str]) -> str | None:
    """Claim the oldest processable delivery and COMMIT the claim.

    `FOR UPDATE SKIP LOCKED` settles which worker gets the row; the claim is
    then committed at once, so no lock is held across the provider reads that
    processing makes. `claimed_at` is what keeps a second worker off it after
    that, until the lease runs out.

    Oldest by the provider's own event time first: processing re-reads the
    provider rather than trusting the payload, so order cannot change an
    outcome, but reading in event order keeps the trail legible.
    """
    from sqlalchemy import text

    from app.db import checkpoint

    row_id = session.execute(text("""
        UPDATE webhook_events SET claimed_at = clock_timestamp(), attempts = attempts + 1
         WHERE id = (SELECT id FROM webhook_events
                      WHERE status = 'RECEIVED' AND signature_valid
                        AND attempts < :max
                        AND NOT (id = ANY(:seen))
                        AND (claimed_at IS NULL
                             OR claimed_at < clock_timestamp() - make_interval(secs => :lease))
                      ORDER BY occurred_at NULLS LAST, received_at, id
                      FOR UPDATE SKIP LOCKED
                      LIMIT 1)
        RETURNING id
    """), {"max": max_attempts, "lease": lease_seconds, "seen": exclude}).scalar()
    # `clock_timestamp()`, not `now()`: `now()` is frozen at the start of the
    # transaction, so a lease measured against it would never run out for a
    # worker that holds one long session.
    checkpoint(session)
    return row_id


def dead_letter_exhausted(session, *, max_attempts: int = MAX_ATTEMPTS,
                          lease_seconds: int = CLAIM_LEASE_SECONDS) -> int:
    """Deliveries out of attempts become FAILED: kept, visible, not retried.

    Not while a claim is live: a delivery on its last attempt may still be
    running in another worker, and dead-lettering it underneath that worker
    would record a failure for a delivery about to succeed."""
    from sqlalchemy import text

    return session.execute(text("""
        UPDATE webhook_events
           SET status = 'FAILED', processed_at = now(),
               processing_note = COALESCE(processing_note || ' ', '')
                   || 'Dead-lettered after ' || attempts || ' processing attempts.'
         WHERE status = 'RECEIVED' AND signature_valid AND attempts >= :max
           AND (claimed_at IS NULL
                OR claimed_at < clock_timestamp() - make_interval(secs => :lease))
    """), {"max": max_attempts, "lease": lease_seconds}).rowcount or 0


def process_pending(session, *, limit: int = 50, adapter=None,
                    lease_seconds: int = CLAIM_LEASE_SECONDS,
                    max_attempts: int = MAX_ATTEMPTS) -> dict:
    """Process acknowledged deliveries. The worker's `webhooks` job.

    Each delivery is processed as WEBHOOK, bound to the merchant our own
    records resolved it to at ingest (never the payload's say-so), so the
    database narrows everything it touches to that merchant.

    Bounded twice: `limit` deliveries per pass, and `max_attempts` per
    delivery. A failure leaves the delivery RECEIVED with its attempt counted;
    the lease is the backoff before it is tried again.
    """
    from app import context
    from app.db import checkpoint

    adapter = adapter or get_adapter(session)
    report = {"processed": 0, "failed": 0, "dead_lettered": 0, "results": []}
    # At most one attempt per delivery per pass. Without this a delivery that
    # fails fast is claimed again at once and spends every attempt it has in
    # one pass -- dead-lettered in milliseconds for what may be a blip.
    seen: list[str] = []

    for _ in range(limit):
        row_id = _claim_next(session, lease_seconds=lease_seconds,
                             max_attempts=max_attempts, exclude=seen)
        if row_id is None:
            break
        seen.append(row_id)
        event = session.get(WebhookEvent, row_id)
        ctx = context.ExecutionContext(
            context.ActorType.WEBHOOK, actor=event.event_id,
            tenant_id=event.tenant_id, merchant_id=event.merchant_id,
            correlation_id=event.correlation_id)
        try:
            with context.bound(ctx, session):
                result = process_event(session, event, adapter=adapter)
            checkpoint(session)
            report["processed"] += 1
            report["results"].append(result)
        except Exception as exc:
            session.rollback()
            event = session.get(WebhookEvent, row_id)
            event.processing_note = (f"Attempt {event.attempts} failed: "
                                     f"{type(exc).__name__}: {exc}")[:500]
            checkpoint(session)
            report["failed"] += 1

    report["dead_lettered"] = dead_letter_exhausted(session, max_attempts=max_attempts,
                                                    lease_seconds=lease_seconds)
    return report

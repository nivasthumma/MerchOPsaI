"""What a verified webhook actually causes — MerchantOps §34, §35.

The webhook decides *when* to look. It never decides *what was found*.

    verified event naming an entity
        -> find the actions that touch that entity
        -> re-read provider state through the adapter   <- the authority
        -> settle, or raise a mismatch incident
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.audit.trace import record_incident
from app.integrations.razorpay.adapter import get_adapter
from app.models import (
    AgentAction, IncidentSeverity, IncidentType, VerificationState,
    WebhookEvent, WebhookStatus,
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
        event.processed_at = datetime.now(timezone.utc)
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
        except Exception as exc:                                    # noqa: BLE001
            # A failed read is not a settlement, and it is certainly not a
            # reason to believe the payload. Leave the action as it was; the
            # reconciliation sweep will try again.
            notes.append(f"{action.id}: provider read failed ({type(exc).__name__})")
            continue

        reverified.append(action.id)

        # §49. An action that came from a recovery candidate settles its plan
        # here, so a paid link is recorded as recovered when the provider says
        # so rather than when someone next asks.
        if action.recovery_candidate_id:
            settled_plans.add(_settle_candidates_plan(session, action, adapter))

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

    event.status = WebhookStatus.PROCESSED
    event.processed_at = datetime.now(timezone.utc)
    plans = sorted(p for p in settled_plans if p)
    if plans:
        notes.append(f"settled plan(s): {', '.join(plans)}")
    event.processing_note = "; ".join(notes) or "No action taken."
    session.flush()

    return IngestResult(WebhookStatus.PROCESSED, event.event_id, stored_id=event.id,
                        note=event.processing_note, reverified=reverified,
                        incident_id=incident_id)

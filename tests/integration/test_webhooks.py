"""Webhook ingestion — MerchantOps §11, §34, §35.

The property under test is not "webhooks are received". It is that a webhook is
**evidence, not authority**: it can make the system go and look, and it can
never tell the system what it found.
"""
from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime
from app.api import security as sec
from app.api.main import app
from app.config import get_settings
from app.models import (
    Incident,
    IncidentType,
    VerificationState,
    WebhookEvent,
    WebhookStatus,
)

SECRET = "whsec_test_only"


@pytest.fixture
def secret(monkeypatch):
    """Configure a webhook secret for the process."""
    s = get_settings()
    monkeypatch.setattr(s, "razorpay_webhook_secret", SECRET, raising=False)
    return SECRET


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def envelope(event: str, *, refund_id="rfnd_X", payment_id="pay_X", status="processed",
             created_at=1787000000):
    """`created_at` is the provider's own event time, and it is a parameter
    because §14's out-of-order case is not expressible without one: a late
    delivery is late relative to when it HAPPENED, not to when it arrived."""
    return {
        "entity": "event", "event": event, "contains": ["refund"],
        "created_at": created_at,
        "payload": {"refund": {"entity": {
            "id": refund_id, "payment_id": payment_id, "amount": 499900,
            "status": status}}},
    }


def deliver(client, body: dict, *, secret_value=SECRET, event_id="evt_001",
            sign=True, raw=None):
    payload = raw if raw is not None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if event_id:
        headers["X-Razorpay-Event-Id"] = event_id
    if sign:
        headers["X-Razorpay-Signature"] = hmac.new(
            secret_value.encode(), payload, hashlib.sha256).hexdigest()
    return client.post("/webhooks/razorpay", content=payload, headers=headers)


# ----------------------------------------------------------------- signature
def test_valid_signature_is_accepted(client, secret):
    r = deliver(client, envelope("refund.processed"))
    assert r.status_code == 200
    assert r.json()["status"] in ("PROCESSED", "IGNORED")


def test_bad_signature_is_stored_but_never_processed(client, secret, db):
    body = json.dumps(envelope("refund.processed")).encode()
    r = client.post("/webhooks/razorpay", content=body, headers={
        "X-Razorpay-Signature": "f" * 64, "X-Razorpay-Event-Id": "evt_bad"})
    assert r.status_code == 200
    assert r.json()["status"] == "INVALID"

    ev = db.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_bad").one()
    assert ev.signature_valid is False
    assert ev.status is WebhookStatus.INVALID
    assert ev.processed_at is None
    # Stored anyway: a rejected delivery that leaves no row is an attack nobody
    # can investigate.
    assert ev.payload_hash


def test_missing_signature_is_rejected(client, secret):
    r = deliver(client, envelope("refund.processed"), sign=False, event_id="evt_nosig")
    assert r.json()["status"] == "INVALID"


def test_signature_covers_the_exact_bytes(client, secret):
    """Signed over one body, delivered with another. Re-serialising the parsed
    JSON server-side would make this pass, which is the classic way this check
    gets written so it never actually checks anything."""
    signed_over = json.dumps(envelope("refund.processed")).encode()
    tampered = json.dumps(envelope("refund.failed")).encode()
    sig = hmac.new(SECRET.encode(), signed_over, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/razorpay", content=tampered, headers={
        "X-Razorpay-Signature": sig, "X-Razorpay-Event-Id": "evt_tamper"})
    assert r.json()["status"] == "INVALID"


def test_without_a_configured_secret_nothing_is_acted_on(client, db):
    """No secret: deliveries are recorded but never processed. Accepting them
    as verified would be a forgery hole; refusing to record them would lose the
    evidence."""
    get_settings().razorpay_webhook_secret = None
    r = deliver(client, envelope("refund.processed"), event_id="evt_nosecret")
    assert r.json()["status"] == "IGNORED"
    ev = db.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_nosecret").one()
    assert ev.signature_valid is False
    assert ev.processed_at is None
    assert "not acted on" in (ev.processing_note or "").lower()


# ---------------------------------------------------------------- dedup
def test_redelivery_is_recorded_once(client, secret, db):
    first = deliver(client, envelope("refund.processed"), event_id="evt_dup")
    second = deliver(client, envelope("refund.processed"), event_id="evt_dup")

    assert first.json()["status"] != "DUPLICATE"
    assert second.json()["status"] == "DUPLICATE"
    assert db.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_dup").count() == 1


def test_dedup_falls_back_to_payload_hash(client, secret, db):
    body = envelope("refund.processed")
    deliver(client, body, event_id=None)
    second = deliver(client, body, event_id=None)
    assert second.json()["status"] == "DUPLICATE"


def test_unsubscribed_event_types_are_recorded_not_routed(client, secret, db):
    r = deliver(client, envelope("subscription.charged"), event_id="evt_sub")
    assert r.json()["status"] == "IGNORED"
    ev = db.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_sub").one()
    assert "no subscriber" in (ev.processing_note or "").lower()


def test_event_for_an_unknown_entity_is_recorded_not_routed(client, secret, db):
    r = deliver(client, envelope("refund.processed", payment_id="pay_nobody",
                                 refund_id="rfnd_nobody"), event_id="evt_unknown")
    assert r.json()["status"] == "IGNORED"


# ------------------------------------------------- evidence, not authority
def _settled_action(db, owner):
    """A refund that genuinely executed and verified SUCCESS."""
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    r = approve_and_execute(db, out.task.id, owner)
    action = r["action"]
    assert action.verification_state is VerificationState.SUCCESS
    # The webhook endpoint opens its own session_scope, so uncommitted work in
    # this one is invisible to it — as it would be to a real provider callback.
    db.commit()
    return action


def test_webhook_triggers_a_read_and_does_not_trust_its_payload(db, owner, client, secret):
    """The centrepiece.

    The action verified SUCCESS. Provider state is then reversed underneath it.
    A webhook arrives *claiming* `refund.processed` — the happy payload. The
    system must re-read, discover the contradiction, and report what it read,
    not what it was told.
    """
    action = _settled_action(db, owner)
    db.execute(text("UPDATE payments SET amount_refunded_minor = 0, refund_status = NULL "
                    "WHERE external_payment_id = :e"),
               {"e": action.external_payment_id})
    db.execute(text("DELETE FROM refunds"))
    db.commit()

    r = deliver(client, envelope("refund.processed",
                                 payment_id=action.external_payment_id,
                                 refund_id=action.external_reference or "rfnd_X",
                                 status="processed"),
                event_id="evt_contradiction")
    body = r.json()

    assert body["status"] == "PROCESSED"
    assert action.id in body["reverified"]

    db.refresh(action)
    # The payload said "processed". The provider says otherwise. The provider wins.
    assert action.verification_state is not VerificationState.SUCCESS


def test_a_contradiction_raises_an_incident_rather_than_correcting_silently(
        db, owner, client, secret):
    action = _settled_action(db, owner)
    db.execute(text("UPDATE payments SET amount_refunded_minor = 0, refund_status = NULL "
                    "WHERE external_payment_id = :e"),
               {"e": action.external_payment_id})
    db.execute(text("DELETE FROM refunds"))
    db.commit()

    r = deliver(client, envelope("refund.processed",
                                 payment_id=action.external_payment_id,
                                 refund_id=action.external_reference or "rfnd_X"),
                event_id="evt_mismatch")
    assert r.json()["incident_id"], "no reconciliation incident was raised"

    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.RECONCILIATION_MISMATCH).one()
    assert inc.severity.value == "CRITICAL"
    assert inc.signals["action_id"] == action.id
    assert inc.signals["internal_state_before"] == "SUCCESS"
    assert inc.revenue_at_risk_minor == action.amount_minor


def test_a_redelivered_contradiction_raises_one_incident(db, owner, client, secret):
    action = _settled_action(db, owner)
    db.execute(text("UPDATE payments SET amount_refunded_minor = 0, refund_status = NULL "
                    "WHERE external_payment_id = :e"),
               {"e": action.external_payment_id})
    db.execute(text("DELETE FROM refunds"))
    db.commit()

    for n in (1, 2, 3):
        deliver(client, envelope("refund.processed",
                                 payment_id=action.external_payment_id,
                                 refund_id=action.external_reference or "rfnd_X"),
                event_id=f"evt_repeat_{n}")

    assert db.query(Incident).filter(
        Incident.incident_type == IncidentType.RECONCILIATION_MISMATCH).count() == 1


def test_an_ordinary_settlement_raises_no_incident(db, owner, client, secret):
    """A webhook confirming what we already believe is not a mismatch."""
    action = _settled_action(db, owner)
    r = deliver(client, envelope("refund.processed",
                                 payment_id=action.external_payment_id,
                                 refund_id=action.external_reference or "rfnd_X"),
                event_id="evt_ok")
    assert r.json()["incident_id"] is None
    assert db.query(Incident).filter(
        Incident.incident_type == IncidentType.RECONCILIATION_MISMATCH).count() == 0
    db.refresh(action)
    assert action.verification_state is VerificationState.SUCCESS


# ---------------------------------------------------------------- isolation
def test_event_store_is_merchant_scoped(db, owner, client, secret):
    action = _settled_action(db, owner)
    deliver(client, envelope("refund.processed",
                             payment_id=action.external_payment_id,
                             refund_id=action.external_reference or "rfnd_X"),
            event_id="evt_scope")

    a = client.get("/webhooks/events", headers=token("USR_A_OWNER")).json()
    b = client.get("/webhooks/events", headers=token("USR_B_OWNER")).json()
    assert any(e["event_id"] == "evt_scope" for e in a["events"])
    assert all(e["event_id"] != "evt_scope" for e in b["events"])


def test_merchant_is_resolved_from_our_records_not_the_payload(db, owner, client, secret):
    """A forged `account_id` must not be able to address another tenant."""
    action = _settled_action(db, owner)
    body = envelope("refund.processed", payment_id=action.external_payment_id,
                    refund_id=action.external_reference or "rfnd_X")
    body["account_id"] = "acc_MERCH_B_PLEASE"
    deliver(client, body, event_id="evt_forged_account")

    ev = db.query(WebhookEvent).filter(WebhookEvent.event_id == "evt_forged_account").one()
    assert ev.merchant_id == "MERCH_A"


# ------------------------------------------------------------- out-of-order
# MerchantOps §14 lists "out-of-order events" among the deliveries webhook
# handling must cope with, and until now nothing tested it — recorded as the
# one real gap in `docs/adversarial-coverage.md`.
#
# The behaviour is right *by construction*: `process_event` never reads the
# payload for truth, it re-reads provider state through the adapter. So a
# stale event triggers a fresh read that reflects current state whatever order
# events arrived in. That is "a webhook decides *when* to look, never *what*
# was found" doing its job.
#
# Which is exactly why it needs a test rather than a comment. The day somebody
# reads `payload["refund"]["entity"]["status"]` because it is right there, a
# stale `refund.failed` overtaking a settled refund would silently regress a
# SUCCESS — a financial claim reversed by a message that was already obsolete
# when it arrived. Nothing else in this suite would notice.

# An hour before the settlement the other events describe. Providers do not
# promise ordering, and a retried delivery can arrive long after the event it
# describes has been superseded.
STALE_TS = 1786996400
CURRENT_TS = 1787000000


def test_a_stale_failure_arriving_late_does_not_regress_a_settled_refund(
        db, owner, client, secret):
    """The one that matters.

    The refund executed and read back SUCCESS. Provider state is left exactly
    as it is — the money really did move. A `refund.failed` then arrives,
    stamped an hour earlier: the provider's own retry of an event that was
    superseded before it was delivered.

    The system must re-read, find SUCCESS, and leave it alone.
    """
    action = _settled_action(db, owner)

    r = deliver(client, envelope("refund.failed",
                                 payment_id=action.external_payment_id,
                                 refund_id=action.external_reference or "rfnd_X",
                                 status="failed", created_at=STALE_TS),
                event_id="evt_stale_failure")
    body = r.json()

    # It was acted on rather than dropped: an out-of-order event is still a
    # reason to go and look, and refusing to look would be its own bug.
    assert body["status"] == "PROCESSED"
    assert action.id in body["reverified"]

    db.refresh(action)
    assert action.verification_state is VerificationState.SUCCESS, (
        "a stale failure payload reversed a verified refund — the payload was "
        "believed instead of the provider")
    # And no incident: the provider and our records agree. A mismatch raised
    # here would be a CRITICAL page for an event that said nothing new.
    assert body["incident_id"] is None
    assert db.query(Incident).filter(
        Incident.incident_type == IncidentType.RECONCILIATION_MISMATCH).count() == 0


@pytest.mark.parametrize("arrival", [
    ("refund.processed", "refund.failed"),
    ("refund.failed", "refund.processed"),
], ids=["in-order", "reversed"])
def test_the_order_deliveries_arrive_in_does_not_change_the_outcome(
        arrival, db, owner, client, secret):
    """The same two events, both arrival orders, one final state.

    This is the invariant §14 is asking for, stated as a property rather than
    as a story about one sequence: the terminal state is a function of what the
    provider says, not of the order in which its messages happened to land.

    Both cases are needed, and a hand-applied "believe the payload" defect shows
    why: with that defect in place `in-order` fails and `reversed` still passes,
    because the last payload to arrive happens to say "processed" and lands on
    the right answer for the wrong reason. One of the two orders is a test; the
    pair is the property.
    """
    action = _settled_action(db, owner)

    for n, event_type in enumerate(arrival):
        stale = event_type.endswith("failed")
        r = deliver(client,
                    envelope(event_type,
                             payment_id=action.external_payment_id,
                             refund_id=action.external_reference or "rfnd_X",
                             status="failed" if stale else "processed",
                             # The failure is always the older event, whichever
                             # order it arrives in. That is what makes one of
                             # these two cases genuinely out-of-order.
                             created_at=STALE_TS if stale else CURRENT_TS),
                    event_id=f"evt_order_{arrival[0][7:]}_{n}")
        # Asserted per delivery, not just at the end. The final state is
        # SUCCESS before this loop runs, so a version of this test that only
        # checked the end state would pass just as well if both deliveries were
        # rejected, ignored, or never routed to the action at all -- proving
        # nothing about ordering. These two lines are what make it a test.
        body = r.json()
        assert body["status"] == "PROCESSED", f"{event_type} was not processed"
        assert action.id in body["reverified"], f"{event_type} did not re-read the action"

    db.refresh(action)
    assert action.verification_state is VerificationState.SUCCESS
    assert db.query(Incident).filter(
        Incident.incident_type == IncidentType.RECONCILIATION_MISMATCH).count() == 0


def test_the_providers_own_timestamp_is_kept_when_deliveries_arrive_reversed(
        db, owner, client, secret):
    """Arrival order is recorded separately from event order, not instead of it.

    Without this, `occurred_at` could quietly become a second copy of
    `received_at` — and the event store would lose the only field that can
    show, after the fact, that a delivery was late.
    """
    action = _settled_action(db, owner)

    for event_id, event_type, ts in (
            ("evt_reversed_new", "refund.processed", CURRENT_TS),
            ("evt_reversed_old", "refund.failed", STALE_TS)):
        deliver(client, envelope(event_type,
                                 payment_id=action.external_payment_id,
                                 refund_id=action.external_reference or "rfnd_X",
                                 status="failed" if ts == STALE_TS else "processed",
                                 created_at=ts),
                event_id=event_id)

    new, old = (db.query(WebhookEvent).filter(WebhookEvent.event_id == e).one()
                for e in ("evt_reversed_new", "evt_reversed_old"))
    # Both timestamps must actually be populated, or the comparisons below are
    # comparing None to None and would pass on a parser that dropped the field.
    assert old.occurred_at is not None and new.occurred_at is not None

    # Arrived second, happened first. Both halves asserted, because either one
    # alone is satisfied by a field that simply copies the other.
    assert old.received_at > new.received_at
    assert old.occurred_at < new.occurred_at

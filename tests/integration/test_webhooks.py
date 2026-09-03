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


def envelope(event: str, *, refund_id="rfnd_X", payment_id="pay_X", status="processed"):
    return {
        "entity": "event", "event": event, "contains": ["refund"],
        "created_at": 1787000000,
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

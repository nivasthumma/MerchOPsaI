"""The notification path — MerchantOps had every control and no way to tell anyone.

The tests that matter here are not "does an email get built". They are:

  - does the right *person* get told, derived from what policy requires
  - can a notification cross a merchant boundary (it must not; an email cannot
    be recalled)
  - does a sweep that runs every two minutes over a fifteen-minute window send
    one chase or eight
  - when a channel fails, is that visible or lost
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.models import (
    Approval,
    NotificationKind,
    NotificationStatus,
    OperatorNotification,
    TaskStatus,
)
from app.notify import consumers
from app.notify.channels import DeliveryRefused
from app.notify.messages import rupees
from app.notify.routing import recipients_for, who_can_perform
from app.notify.service import retry_pending
from app.notify.sweep import sweep_approvals


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _task(session, merchant_id="MERCH_A", user_id="USR_A_OWNER") -> str:
    from app.models import AgentTask

    tid = f"TASK_NTF_{uuid.uuid4().hex[:8].upper()}"
    session.add(AgentTask(
        id=tid, merchant_id=merchant_id, user_id=user_id,
        request="notification probe", status=TaskStatus.AWAITING_APPROVAL,
        agent_version="t", model_version="t", prompt_version="t"))
    session.flush()
    return tid


def _approval(session, *, merchant_id="MERCH_A", action_type="request_refund",
              risk="HIGH", expires_in=900, amount_minor=450000) -> Approval:
    ap = Approval(
        id=f"APR_NTF_{uuid.uuid4().hex[:8].upper()}",
        task_id=_task(session, merchant_id=merchant_id,
                      user_id="USR_A_OWNER" if merchant_id == "MERCH_A" else "USR_B_OWNER"),
        merchant_id=merchant_id, action_type=action_type,
        action_payload={"synthetic_payment_id": "PAY_X", "amount_minor": amount_minor},
        evidence=[], risk_level=risk, decision="PENDING", required_signatures=1,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in))
    session.add(ap)
    session.flush()
    return ap


def _notifications(session, **where) -> list[OperatorNotification]:
    q = select(OperatorNotification)
    for k, v in where.items():
        q = q.where(getattr(OperatorNotification, k) == v)
    return list(session.execute(q.order_by(OperatorNotification.created_at)).scalars())


# --------------------------------------------------------------------------
# routing — the part that must not be wrong
# --------------------------------------------------------------------------
def test_an_approval_goes_to_whoever_could_perform_the_action(db):
    """`request_refund` needs `action:refund`. The owner and the approver hold
    it; the analyst does not, and telling the analyst to approve something they
    cannot approve is worse than not telling them."""
    who = who_can_perform(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                          action_type="request_refund")
    ids = {r.user_id for r in who}
    assert "USR_A_OWNER" in ids
    assert "USR_A_APPROVER" in ids
    assert "USR_A_ANALYST" not in ids


def test_routing_follows_the_permission_model_rather_than_a_second_list(db):
    """Take the permission away and the person stops being notified, without
    anything in app/notify changing. That is the whole reason routing derives
    from `required_permissions` instead of keeping its own list."""
    db.execute(text("UPDATE users SET permissions = :p WHERE id = 'USR_A_OWNER'"),
               {"p": '["read:metrics", "read:orders"]'})
    db.flush()
    who = who_can_perform(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                          action_type="request_refund")
    assert "USR_A_OWNER" not in {r.user_id for r in who}


def test_no_recipient_crosses_a_merchant_boundary(db):
    """An email is the one artefact that cannot be recalled."""
    for action in ("request_refund", "generate_payment_link"):
        a = who_can_perform(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                            action_type=action)
        b = who_can_perform(db, tenant_id="TEN_NORTHWIND", merchant_id="MERCH_B",
                            action_type=action)
        assert {r.user_id for r in a}.isdisjoint({r.user_id for r in b})
        assert all(r.email.endswith("@kettle.example") for r in a)
        assert all(r.email.endswith("@northwind.example") for r in b)


def test_the_tenant_boundary_is_checked_too(db):
    """MERCH_A's users are in TEN_KETTLE. Asking for them under the wrong tenant
    returns nobody -- the outer boundary is not decorative."""
    assert who_can_perform(db, tenant_id="TEN_NORTHWIND", merchant_id="MERCH_A",
                           action_type="request_refund") == []


def test_an_unknown_action_type_routes_to_nobody_rather_than_everybody(db):
    """`required_permissions` returns [] for a type the registry does not know,
    and "requires nothing" must not become "send it to the whole merchant"."""
    assert who_can_perform(db, tenant_id="TEN_KETTLE", merchant_id="MERCH_A",
                           action_type="not_a_tool") == []


def test_news_goes_to_everyone_attached_to_the_merchant(db):
    """An incident is not a request for authority, so it is not gated on one."""
    who = recipients_for(db, NotificationKind.INCIDENT_OPENED,
                         tenant_id="TEN_KETTLE", merchant_id="MERCH_A")
    assert {"USR_A_OWNER", "USR_A_ANALYST", "USR_A_APPROVER"} <= {r.user_id for r in who}


# --------------------------------------------------------------------------
# the send path
# --------------------------------------------------------------------------
def test_an_approval_request_notifies_the_approvers(db):
    ap = _approval(db)
    consumers.on_approval_requested(
        db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))

    sent = _notifications(db, subject_id=ap.id)
    assert sent, "an approval was raised and nobody was told"
    assert {n.recipient for n in sent} == {"owner@kettle.example", "approver@kettle.example"}
    assert all(n.status is NotificationStatus.SENT for n in sent)


def test_the_message_says_what_it_is_and_when_it_dies(db):
    ap = _approval(db, risk="CRITICAL", amount_minor=4500000)
    consumers.on_approval_requested(
        db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))
    n = _notifications(db, subject_id=ap.id)[0]

    assert "₹45,000.00" in n.title, "paise must not reach a human"
    assert "CRITICAL" in n.title
    assert "expires" in n.body
    assert "Two approvers are required" in n.body
    assert n.severity == "CRITICAL"


def test_a_notification_is_recorded_before_it_is_attempted(db, monkeypatch):
    """A process killed mid-send must leave evidence. The row exists with an
    attempt counted even though the channel never returned."""
    class Dying:
        name = "log"

        def send(self, message):
            raise KeyboardInterrupt("killed mid-send")

    monkeypatch.setattr("app.notify.service.build_channels", lambda: {"log": Dying()})
    ap = _approval(db)
    with pytest.raises(KeyboardInterrupt):
        consumers.on_approval_requested(
            db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))

    rows = _notifications(db, subject_id=ap.id)
    assert rows, "the send died and left no trace of having been tried"
    assert rows[0].status is NotificationStatus.PENDING
    assert rows[0].attempts == 1


def test_a_failing_channel_is_visible_not_lost(db, monkeypatch):
    class Broken:
        name = "log"

        def send(self, message):
            raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr("app.notify.service.build_channels", lambda: {"log": Broken()})
    ap = _approval(db)
    consumers.on_approval_requested(
        db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))

    rows = _notifications(db, subject_id=ap.id)
    assert all(n.status is NotificationStatus.FAILED for n in rows)
    assert "ConnectionRefusedError" in rows[0].last_error


def test_a_refusal_is_suppressed_not_failed(db, monkeypatch):
    """A channel declining on purpose is not an outage, and counting it as one
    would make an outage impossible to see."""
    class Quiet:
        name = "log"

        def send(self, message):
            raise DeliveryRefused("quiet hours")

    monkeypatch.setattr("app.notify.service.build_channels", lambda: {"log": Quiet()})
    ap = _approval(db)
    consumers.on_approval_requested(
        db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))
    assert all(n.status is NotificationStatus.SUPPRESSED
               for n in _notifications(db, subject_id=ap.id))


def test_a_failed_notification_is_retried_and_settles(db, monkeypatch):
    state = {"fail": True}

    class Flaky:
        name = "log"

        def send(self, message):
            if state["fail"]:
                raise ConnectionRefusedError("smtp down")

    monkeypatch.setattr("app.notify.service.build_channels", lambda: {"log": Flaky()})
    ap = _approval(db)
    consumers.on_approval_requested(
        db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))
    assert all(n.status is NotificationStatus.FAILED
               for n in _notifications(db, subject_id=ap.id))

    state["fail"] = False
    retry_pending(db)
    rows = _notifications(db, subject_id=ap.id)
    assert all(n.status is NotificationStatus.SENT for n in rows)
    assert all(n.attempts == 2 for n in rows)


# --------------------------------------------------------------------------
# the sweep, and the reason dedupe is a constraint
# --------------------------------------------------------------------------
def test_the_sweep_chases_an_approval_about_to_expire(db):
    ap = _approval(db, expires_in=120)          # inside the 300s warning window
    sweep_approvals(db)
    rows = _notifications(db, subject_id=ap.id, kind=NotificationKind.APPROVAL_EXPIRING)
    assert rows
    assert "expires" in rows[0].body and "minute" in rows[0].body
    assert rows[0].severity == "CRITICAL", "a chase is the urgent one by definition"


def test_an_approval_with_time_left_is_not_chased(db):
    ap = _approval(db, expires_in=900)          # outside the warning window
    sweep_approvals(db)
    assert not _notifications(db, subject_id=ap.id,
                              kind=NotificationKind.APPROVAL_EXPIRING)


def test_running_the_sweep_repeatedly_sends_one_chase(db):
    """The sweep wants a two-minute cadence against a fifteen-minute window, so
    it recomputes the same 'expiring soon' seven times. The UNIQUE constraint is
    what makes that safe to tune -- an if-statement would be bypassed by a
    concurrent drain."""
    ap = _approval(db, expires_in=120)
    for _ in range(8):
        sweep_approvals(db)

    rows = _notifications(db, subject_id=ap.id, kind=NotificationKind.APPROVAL_EXPIRING)
    per_recipient = {}
    for n in rows:
        per_recipient[n.recipient] = per_recipient.get(n.recipient, 0) + 1
    assert set(per_recipient.values()) == {1}, per_recipient


def test_an_expired_approval_is_reported_as_what_did_not_happen(db):
    ap = _approval(db, expires_in=-60)
    sweep_approvals(db)
    rows = _notifications(db, subject_id=ap.id, kind=NotificationKind.APPROVAL_EXPIRED)
    assert rows
    assert "Nothing was executed" in rows[0].body
    assert rows[0].severity == "WARNING"


def test_a_decided_approval_is_neither_chased_nor_mourned(db):
    ap = _approval(db, expires_in=-60)
    ap.decision = "APPROVED"
    db.flush()
    sweep_approvals(db)
    assert not _notifications(db, subject_id=ap.id)


def test_the_sweep_does_not_notify_across_merchants(db):
    """The one that would be a breach rather than a bug."""
    a = _approval(db, merchant_id="MERCH_A", expires_in=120)
    b = _approval(db, merchant_id="MERCH_B", expires_in=120)
    sweep_approvals(db)

    for n in _notifications(db, subject_id=a.id):
        assert n.merchant_id == "MERCH_A"
        assert n.recipient.endswith("@kettle.example")
    for n in _notifications(db, subject_id=b.id):
        assert n.merchant_id == "MERCH_B"
        assert n.recipient.endswith("@northwind.example")


# --------------------------------------------------------------------------
# incidents
# --------------------------------------------------------------------------
def test_only_high_and_critical_incidents_interrupt_anyone(db):
    from app.models import Incident, IncidentSeverity, IncidentStatus, IncidentType

    made = []
    for sev in (IncidentSeverity.LOW, IncidentSeverity.HIGH):
        inc = Incident(
            id=f"INC_NTF_{uuid.uuid4().hex[:8].upper()}",
            merchant_id="MERCH_A",
            incident_type=IncidentType.PAYMENT_DEGRADATION, severity=sev,
            status=IncidentStatus.DETECTED, title="t", summary="s",
            detection_key=f"ntf-{uuid.uuid4().hex[:8]}",
            correlation_id=f"COR_NTF_{uuid.uuid4().hex[:8].upper()}",
            revenue_at_risk_minor=250000, detection_rule="probe",
            detection_version="1", started_at=datetime.now(UTC),
            detected_at=datetime.now(UTC))
        db.add(inc)
        db.flush()
        made.append((sev, inc))
        consumers.on_incident_created(
            db, _Event(payload={}, merchant_id="MERCH_A", incident_id=inc.id))

    low, high = made
    assert not _notifications(db, subject_id=low[1].id)
    assert _notifications(db, subject_id=high[1].id)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
@pytest.mark.parametrize("seconds,expected", [
    (-30, "already expired"), (0, "already expired"),
    (45, "in 45 seconds"), (89, "in 89 seconds"),
    (90, "in 1 minute"), (120, "in 2 minutes"),
    (5340, "in 89 minutes"), (5400, "in 1 hour"), (7200, "in 2 hours"),
])
def test_the_countdown_reads_like_a_deadline(seconds, expected):
    """Asserted against a fixed clock. The integration test above cannot pin
    this because wall time moves between building an approval and rendering it,
    and a test that fails at a minute boundary is a test people delete."""
    from app.notify.messages import _countdown

    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert _countdown(now + timedelta(seconds=seconds), now) == expected


@pytest.mark.parametrize("minor,expected", [
    (0, "₹0.00"), (5000, "₹50.00"), (450000, "₹4,500.00"),
    (4500000, "₹45,000.00"), (123456789, "₹12,34,567.89"),
])
def test_money_is_rendered_the_way_an_indian_merchant_reads_it(minor, expected):
    assert rupees(minor) == expected


def test_a_secret_in_an_action_payload_does_not_reach_an_inbox(db):
    """An email is a wider audience than the audit table, so it cannot be the
    laxer of the two."""
    ap = _approval(db)
    ap.action_payload = {**ap.action_payload, "api_key": "rzp_test_supersecret"}
    db.flush()
    consumers.on_approval_requested(
        db, _Event(payload={"approval_id": ap.id}, merchant_id="MERCH_A"))
    body = _notifications(db, subject_id=ap.id)[0].body
    assert "supersecret" not in body


# --------------------------------------------------------------------------
class _Event:
    """The two fields the consumers read off a bus event."""

    def __init__(self, *, payload, merchant_id=None, incident_id=None):
        self.payload = payload
        self.merchant_id = merchant_id
        self.incident_id = incident_id

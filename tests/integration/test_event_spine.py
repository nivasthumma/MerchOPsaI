"""The transactional outbox and event bus — MerchantOps v2 §11, §12, §13.

v2 §12 states the bug the outbox exists to prevent as two lines:

    Database update = success
    Event publishing = failure

So the assertions here are about atomicity and ordering, not about whether a
row can be inserted. Specifically: an event cannot outlive a rolled-back write,
a write cannot commit without its event, delivery is at-least-once rather than
at-most-once, and a broken consumer cannot take the audit trail down with it.
"""
from __future__ import annotations

import pytest

from app.audit.trace import record_incident
from app.detection import detect
from app.events import bus
from app.events.bus import PostgresEventStore, UnknownEventType, drain, publish
from app.events.vocabulary import EVENT_TYPES
from app.models import EventOutbox, Incident, OutboxStatus


@pytest.fixture(autouse=True)
def _clean_consumers():
    """Consumers are a module global; a test that registers one must not leak it."""
    saved = {k: list(v) for k, v in bus._CONSUMERS.items()}
    yield
    bus._CONSUMERS.clear()
    bus._CONSUMERS.update(saved)


# ------------------------------------------------------------ the v2 §12 claim
def test_the_event_is_written_in_the_callers_transaction(db):
    """Not after it, and not in a transaction of its own.

    This is the entire guarantee. If `publish` opened its own session the event
    would survive a rollback of the business state it describes, which is the
    mirror image of the bug §12 names and just as wrong.
    """
    sp = db.begin_nested()
    publish(db, "incident.created", merchant_id="MERCH_A",
            payload={"incident_id": "INC_ROLLED_BACK"})
    assert db.query(EventOutbox).filter_by(merchant_id="MERCH_A").count() == 1
    sp.rollback()

    # The write went away, and the event went with it.
    surviving = [r for r in db.query(EventOutbox).all()
                 if (r.payload or {}).get("incident_id") == "INC_ROLLED_BACK"]
    assert surviving == []


def test_detection_commits_the_incident_and_its_event_together(db):
    """The real path: a detection sweep creates incidents and stream frames."""
    before = db.query(EventOutbox).count()
    report = detect(db, "MERCH_A")
    assert report.incidents_created > 0

    created = db.query(EventOutbox).filter_by(
        event_type="incident.created").all()
    assert len(created) == report.incidents_created
    assert before + report.incidents_created <= db.query(EventOutbox).count()

    # Every frame carries what v2 §11 says an event carries.
    for row in created:
        assert row.merchant_id == "MERCH_A"
        assert row.incident_id is not None
        assert row.correlation_id is not None
        assert row.payload_hash and len(row.payload_hash) == 64
        assert row.schema_version == "v1"
        assert row.status is OutboxStatus.PENDING


# ------------------------------------------------------------------- vocabulary
def test_an_unknown_event_type_is_refused_at_the_call_site(db):
    """v2 §62's list is closed, so a typo is an error rather than a silent hole."""
    with pytest.raises(UnknownEventType) as exc:
        publish(db, "incident_created", merchant_id="MERCH_A")   # snake_case typo
    assert "§62" in str(exc.value)
    assert db.query(EventOutbox).filter_by(event_type="incident_created").count() == 0


def test_every_declared_event_type_can_actually_be_published(db):
    """Guards against a name in the vocabulary that nothing validates."""
    for event_type in EVENT_TYPES:
        publish(db, event_type, merchant_id="MERCH_A")
    assert db.query(EventOutbox).count() >= len(EVENT_TYPES)


# ---------------------------------------------------------------------- drain
def test_drain_delivers_pending_events_oldest_first(db):
    seen: list[str] = []
    bus.subscribe("incident.created", lambda s, e: seen.append(e.payload["n"]))

    for n in ("first", "second", "third"):
        publish(db, "incident.created", merchant_id="MERCH_A", payload={"n": n})

    result = drain(db)
    assert result["published"] == 3
    assert seen == ["first", "second", "third"]

    # Drained rows are not re-delivered.
    assert drain(db)["claimed"] == 0


def test_an_event_with_no_consumer_is_still_marked_published(db):
    """Otherwise the table grows forever until the UI subscribes to everything."""
    publish(db, "hypothesis.rejected", merchant_id="MERCH_A")
    assert drain(db)["published"] >= 1
    row = db.query(EventOutbox).filter_by(event_type="hypothesis.rejected").one()
    assert row.status is OutboxStatus.PUBLISHED
    assert row.published_at is not None


def test_a_failing_consumer_does_not_block_the_events_behind_it(db):
    def explode(session, event):
        raise RuntimeError("consumer is broken")

    delivered: list[str] = []
    bus.subscribe("hypothesis.created", explode)
    bus.subscribe("incident.resolved", lambda s, e: delivered.append(e.id))

    publish(db, "hypothesis.created", merchant_id="MERCH_A")
    publish(db, "incident.resolved", merchant_id="MERCH_A")

    result = drain(db)
    assert result["failed"] == 1
    assert result["published"] == 1
    assert len(delivered) == 1          # the good one still went through

    bad = db.query(EventOutbox).filter_by(event_type="hypothesis.created").one()
    assert bad.status is OutboxStatus.PENDING     # retried, not lost
    assert bad.attempts == 1
    assert "consumer is broken" in bad.last_error


def test_a_consumer_that_never_recovers_goes_dead_rather_than_looping(db):
    bus.subscribe("agent.started", lambda s, e: (_ for _ in ()).throw(ValueError("no")))
    publish(db, "agent.started", merchant_id="MERCH_A")

    for _ in range(3):
        drain(db)

    row = db.query(EventOutbox).filter_by(event_type="agent.started").one()
    assert row.status is OutboxStatus.DEAD
    assert row.attempts == 3
    # Kept, not deleted: a broken consumer should be visible in the table.
    assert row.last_error is not None


# --------------------------------------------------------------------- reading
def test_since_pages_forward_without_repeating_or_skipping(db):
    """The cursor is an event id, because timestamps collide inside one transaction."""
    for n in range(6):
        publish(db, "tool.completed", merchant_id="MERCH_A", payload={"n": n})

    store = PostgresEventStore()
    first = store.since(db, merchant_id="MERCH_A", limit=4)
    assert [e.payload["n"] for e in first] == [0, 1, 2, 3]

    rest = store.since(db, after=first[-1].id, merchant_id="MERCH_A", limit=4)
    assert [e.payload["n"] for e in rest] == [4, 5]


def test_the_store_does_not_cross_merchants(db):
    publish(db, "incident.created", merchant_id="MERCH_A", payload={"who": "a"})
    publish(db, "incident.created", merchant_id="MERCH_B", payload={"who": "b"})

    store = PostgresEventStore()
    assert {e.payload["who"] for e in store.since(db, merchant_id="MERCH_A")} == {"a"}
    assert {e.payload["who"] for e in store.since(db, merchant_id="MERCH_B")} == {"b"}


# ------------------------------------------------------- audit / stream mirror
def test_the_audit_trail_survives_a_broken_event_stream(db, monkeypatch):
    """An audit row is an obligation; a stream frame is a convenience.

    If raising the frame could fail the transaction, a bug in the event spine
    would erase the record of what the system did — which is exactly backwards.
    """
    def boom(*a, **k):
        raise RuntimeError("bus is down")
    monkeypatch.setattr("app.events.bus.publish", boom)

    inc = db.query(Incident).first() or _any_incident(db)
    entry = record_incident(db, inc, "incident_detected", {"x": 1})
    assert entry.id is not None                    # the audit row landed anyway


def test_a_resolved_incident_raises_the_resolved_frame_and_others_do_not(db):
    """`incident.resolved` is a derived frame — one particular status change."""
    inc = _any_incident(db)

    record_incident(db, inc, "incident_status_changed", {"to": "TRIAGED"})
    assert db.query(EventOutbox).filter_by(event_type="incident.resolved").count() == 0

    record_incident(db, inc, "incident_status_changed", {"to": "RESOLVED"})
    assert db.query(EventOutbox).filter_by(event_type="incident.resolved").count() == 1


def _any_incident(db) -> Incident:
    detect(db, "MERCH_A")
    inc = db.query(Incident).first()
    assert inc is not None
    return inc


# ------------------------------------------------------------------- endpoints
@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def _token(user_id: str) -> dict:
    from app.api import security as sec
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def test_events_endpoint_pages_and_reports_the_backlog(client, db):
    detect(db, "MERCH_A")

    r = client.get("/events?limit=2", headers=_token("USR_A_OWNER"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["events"]) <= 2
    assert body["next_cursor"] == body["events"][-1]["id"]
    # Nothing has drained yet, so everything raised is still pending.
    assert body["pending"] >= len(body["events"])

    nxt = client.get(f"/events?after={body['next_cursor']}",
                     headers=_token("USR_A_OWNER")).json()
    assert {e["id"] for e in nxt["events"]}.isdisjoint({e["id"] for e in body["events"]})


def test_the_stream_is_scoped_by_the_token_not_by_a_parameter(client, db):
    """A stream is still an authorised read — MerchantOps §54, §57."""
    publish(db, "incident.created", merchant_id="MERCH_B", payload={"secret": "B"})
    db.commit()

    seen = client.get("/events", headers=_token("USR_A_OWNER")).json()
    assert all(e["merchant_id"] == "MERCH_A" for e in seen["events"])
    assert not any((e["payload"] or {}).get("secret") == "B" for e in seen["events"])


def test_the_stream_emits_sse_frames_a_browser_can_resume_from(client, db):
    detect(db, "MERCH_A")
    db.commit()

    r = client.get("/events/stream?seconds=1", headers=_token("USR_A_OWNER"))
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # `id:` is what EventSource sends back as Last-Event-ID; without it a
    # reconnect replays from the beginning or not at all.
    assert "id: EVT_" in body
    assert "event: incident.created" in body
    assert "retry: " in body


def test_drain_endpoint_moves_events_out_of_pending(client, db):
    detect(db, "MERCH_A")
    db.commit()

    before = client.get("/events", headers=_token("USR_A_OWNER")).json()["pending"]
    assert before > 0

    r = client.post("/events/drain", headers=_token("USR_A_OWNER"))
    assert r.status_code == 200, r.text
    assert r.json()["published"] > 0

    after = client.get("/events", headers=_token("USR_A_OWNER")).json()["pending"]
    assert after < before

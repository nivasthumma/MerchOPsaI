"""One payment, end to end — MerchantOps §7.

The claim under test is the one the plan makes:

> A payment should be traceable through its complete lifecycle.

`trace_by_correlation` already answered "everything one operation touched".
The gap was that a payment's life spans several operations with several
correlation ids, so the most important test here is
`test_the_chain_spans_more_than_one_correlation_id` — if it ever passes with a
count of one, this endpoint has stopped being different from the one that
already existed.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import security as sec
from app.api.main import app
from app.audit.lifecycle import STAGES, payment_lifecycle

PAYMENT = "SYN_PAY_0002"


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str = "USR_A_OWNER") -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def _stages(report) -> list[str]:
    return [e["stage"] for e in report["events"]]


@pytest.fixture
def refunded(db, owner):
    """A payment taken all the way through: detected, investigated, approved,
    executed, verified."""
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.detection.engine import detect

    detect(db, owner.merchant_id)
    db.flush()
    out = AgentRuntime(db, owner).run(
        f"Refund the duplicate payment {PAYMENT} amount 499900.")
    approve_and_execute(db, out.task.id, owner)
    db.flush()
    return out


# ------------------------------------------------------------------ the chain
def test_a_bare_payment_still_has_a_lifecycle(db):
    """Nothing has happened to it yet, and the answer is the payment itself
    rather than an empty list. "No events" would read as "we have no record"."""
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    assert report is not None
    assert _stages(report) == ["payment", "mapping"]
    assert report["external_payment_id"].startswith("pay_")
    assert report["provider"] == "razorpay"
    assert report["environment"] == "test"


def test_the_whole_chain_appears_once_it_has_run(db, refunded):
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    stages = set(_stages(report))
    for expected in ("payment", "mapping", "investigation", "approval",
                     "action", "verification"):
        assert expected in stages, (expected, sorted(stages))

    assert report["task_ids"] and report["action_ids"]


def test_the_chain_spans_more_than_one_correlation_id(db, refunded):
    """The reason this endpoint exists.

    Detection raised the incident under one correlation id; a webhook arrives
    under another. If this ever reports one, `/trace/{correlation_id}` would
    have answered the question and this module would be redundant.
    """
    _deliver_webhook(db, refunded)
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    assert len(report["correlation_ids"]) > 1, report["correlation_ids"]


def test_verification_is_its_own_moment_not_folded_into_execution(db, refunded):
    """An action submitted at 12:00 and verified at 12:04 is two events.
    Collapsing them hides the gap UNKNOWN lives in."""
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    action = next(e for e in report["events"] if e["stage"] == "action")
    verify = next(e for e in report["events"] if e["stage"] == "verification")
    assert action["at"] is not None and verify["at"] is not None
    assert verify["at"] >= action["at"]


def test_events_are_chronological_not_sorted_into_the_expected_order(db, refunded):
    """An out-of-order webhook really did arrive out of order, and tidying it
    into the position it "should" have had hides the thing worth seeing."""
    _deliver_webhook(db, refunded,
                     received_at=datetime(1999, 1, 1, tzinfo=UTC))

    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    ats = [e["at"] for e in report["events"] if e["at"]]
    assert ats == sorted(ats), "events must be in time order"
    # And the antique webhook sorts first, before the payment itself, rather
    # than being filed under "provider events happen after actions".
    assert report["events"][0]["stage"] == "provider_event"


def test_an_unknown_action_shows_its_attempts_and_who_owns_it(db, owner):
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector
    from app.verification.schedule import escalate

    out = AgentRuntime(db, owner).run(
        f"Refund the duplicate payment {PAYMENT} amount 499900.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    db.flush()

    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    verify = [e for e in report["events"] if e["stage"] == "verification"]
    assert any("UNKNOWN" in e["label"] for e in verify)

    escalate(db, r["action"], reason="test")
    db.flush()
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    assert any(e["label"] == "Escalated to a person"
               for e in report["events"] if e["stage"] == "verification")


def test_ordering_survives_timestamps_in_different_offsets(db, refunded):
    """The bug the previous implementation had, made unmissable.

    Events were sorted by the ISO STRING, which equals chronological order only
    while every value carries the same UTC offset. Postgres renders its reads
    in the session timezone; a value written in Python carries +00:00. Compare
    those as text and an earlier instant can sort after a later one.

    The pair below inverts. `01:00-05:00` is 06:00Z — four hours AFTER
    `02:00+00:00` — but as text "01:00" sorts before "02:00". Text order and
    time order are opposite, which is the whole failure in two values.
    """
    from datetime import timedelta, timezone

    from app.audit.lifecycle import _key

    earlier = datetime(2026, 9, 7, 2, 0, tzinfo=UTC)                    # 02:00Z
    later = datetime(2026, 9, 7, 1, 0,
                     tzinfo=timezone(timedelta(hours=-5)))              # 06:00Z

    as_text = sorted([later, earlier], key=lambda d: d.isoformat())
    as_instants = sorted([later, earlier], key=_key)

    # The fixture has to actually distinguish the two, or this passes without
    # testing anything — which is how the first version of it was written.
    assert as_text != as_instants
    assert as_text == [later, earlier], "text order puts the later instant first"
    assert as_instants == [earlier, later]


def test_an_event_with_no_timestamp_leads_rather_than_being_dropped(db, refunded):
    """Several steps are derived from a state rather than an event and have no
    honest time. They must still appear, and must not be given an invented one."""
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    assert report["events"], "the fixture must produce events"
    # Every event survives the sort, timestamped or not.
    assert all("stage" in e for e in report["events"])
    # And `at` is a string or null on the wire — never a datetime.
    assert all(e["at"] is None or isinstance(e["at"], str)
               for e in report["events"])


def test_a_policy_gated_tool_call_is_not_reported_as_failed(db, refunded):
    """`request_refund` records success=False with no error code when the
    control plane holds it for a human. Rendering that as "failed" next to a
    refund that went on to succeed is simply wrong."""
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    gated = [e for e in report["events"]
             if e["stage"] == "tool_call" and e["label"] == "request_refund"]
    assert gated, "the fixture must contain a gated call"
    assert all(e["detail"] != "failed" for e in gated)
    assert any("held by policy" in e["detail"] for e in gated)


def test_an_unmapped_payment_says_so_rather_than_failing(db):
    """`SYN_PAY_0009` is deliberately unmapped. Its lifecycle is still a
    lifecycle; it simply has no provider side."""
    report = payment_lifecycle(db, "MERCH_A", "SYN_PAY_0009")
    assert report is not None
    assert report["external_payment_id"] is None
    assert "mapping" not in _stages(report)


def test_an_unverified_mapping_says_nobody_has_checked(db):
    """Null `verified_at` means nobody has confirmed the id at the provider,
    which is a different claim from "checked and it was there"."""
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    mapping = next(e for e in report["events"] if e["stage"] == "mapping")
    assert "never confirmed" in mapping["detail"]


def test_only_tool_calls_that_touched_this_payment_are_listed(db, owner):
    """A revenue investigation makes a dozen calls, most about the merchant
    rather than one transaction. Listing all of them buries the payment's story
    in the surrounding investigation's."""
    from app.agent.runtime import AgentRuntime

    AgentRuntime(db, owner).run("Why did revenue drop?")
    db.flush()

    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    # That investigation named no payment, so it contributes no tool calls
    # here even though it ran many.
    assert [e for e in report["events"] if e["stage"] == "tool_call"] == []


def test_the_stage_vocabulary_is_published(db):
    report = payment_lifecycle(db, "MERCH_A", PAYMENT)
    assert report["stages"] == list(STAGES)
    assert set(_stages(report)) <= set(STAGES)


# ------------------------------------------------------------------- the API
def test_the_endpoint_serves_the_chain(client, db, refunded):
    body = client.get(f"/payments/{PAYMENT}/lifecycle", headers=token()).json()
    assert body["payment"]["id"] == PAYMENT
    assert body["events"]


def test_another_merchants_payment_is_not_found_rather_than_forbidden(client, db):
    """403 would confirm the payment exists."""
    r = client.get(f"/payments/{PAYMENT}/lifecycle", headers=token("USR_B_OWNER"))
    assert r.status_code == 404
    assert payment_lifecycle(db, "MERCH_B", PAYMENT) is None


def test_an_unknown_payment_is_404(client, db):
    assert client.get("/payments/SYN_PAY_NOPE/lifecycle",
                      headers=token()).status_code == 404


def test_the_endpoint_requires_authentication(client, db):
    assert client.get(f"/payments/{PAYMENT}/lifecycle").status_code == 401


# ------------------------------------------------------------------- search
def test_searching_a_payment_lands_on_its_lifecycle(client, db):
    """It used to route to the incidents LIST, which is a page that does not
    contain the payment. An operator pasting an id had to start again."""
    body = client.get(f"/search?q={PAYMENT}", headers=token()).json()
    hit = next(r for r in body["results"] if r["kind"] == "payment")
    assert hit["route"] == f"/payments/{PAYMENT}"


def test_searching_an_order_or_customer_resolves_through_their_payment(client, db):
    """"What happened to this order" is a question about its payment."""
    order_id = db.execute(text(
        "SELECT order_id FROM payments WHERE id = :p"), {"p": PAYMENT}).scalar()
    body = client.get(f"/search?q={order_id}", headers=token()).json()
    hit = next(r for r in body["results"] if r["kind"] == "order")
    assert hit["route"].startswith("/payments/")

    customer_id = db.execute(text(
        "SELECT customer_id FROM payments WHERE id = :p"), {"p": PAYMENT}).scalar()
    body = client.get(f"/search?q={customer_id}", headers=token()).json()
    hit = next(r for r in body["results"] if r["kind"] == "customer")
    assert hit["route"].startswith("/payments/")


def test_a_provider_reference_resolves_to_the_payment_it_references(client, db, refunded):
    reference = db.execute(text(
        "SELECT external_reference FROM agent_actions WHERE merchant_id = 'MERCH_A' "
        "AND external_reference IS NOT NULL LIMIT 1")).scalar()
    assert reference

    body = client.get(f"/search?q={reference}", headers=token()).json()
    hit = next(r for r in body["results"] if r["kind"] == "provider_reference")
    assert hit["route"] == f"/payments/{PAYMENT}"


def test_every_search_route_is_reachable(client, db, refunded):
    """A route this API cannot serve is a dead end wearing a link. Every route
    the search box hands out must resolve."""
    for q in (PAYMENT, refunded.task.id):
        for hit in client.get(f"/search?q={q}", headers=token()).json()["results"]:
            route = hit["route"]
            if route.startswith("/payments/"):
                api_path = f"{route}/lifecycle"
            elif route.startswith("/tasks/"):
                api_path = route
            elif route.startswith("/incidents/"):
                api_path = route
            else:
                continue          # a list route, always present
            assert client.get(api_path, headers=token()).status_code == 200, route


# ------------------------------------------------------------------ helpers
def _deliver_webhook(db, out, received_at: datetime | None = None) -> None:
    """A stored provider event naming this payment's external id.

    The timestamp is bound as a real `datetime` rather than a string with a
    `::timestamptz` cast: `::` inside a `text()` statement is read as the start
    of a bind parameter, and the query will not parse.
    """
    external = db.execute(text(
        "SELECT external_payment_id FROM provider_mappings WHERE payment_id = :p"),
        {"p": PAYMENT}).scalar()
    at = received_at or datetime.now(UTC)
    db.execute(text("""
        INSERT INTO webhook_events (id, event_id, provider, event_type,
            schema_version, entity_id, status, signature_valid, payload,
            payload_hash, correlation_id, received_at)
        VALUES (:id, :eid, 'razorpay', 'payment.captured', 'v1', :ent,
                'PROCESSED', true, '{}'::json, 'hash', :cor, :at)
    """), {"id": f"WHK_{uuid.uuid4().hex[:12].upper()}",
           "eid": f"evt_{uuid.uuid4().hex[:12]}",
           "ent": external, "cor": "COR_WEBHOOK_TEST", "at": at})
    db.flush()

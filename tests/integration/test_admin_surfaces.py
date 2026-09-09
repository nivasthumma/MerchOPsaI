"""Policy, the audit trail and provider health, as things a person can read.

§41, §28 and §26. All three existed as behaviour and none as a surface: the
policy engine could be read only by reading the engine, the trail was visible
only beside the object it concerned, and provider health was one verdict inside
a readiness probe.

The seeded dataset has no audit rows and no agent actions, so the tests that
need either write them first. A test that passes because there was nothing to
look at is the failure mode this repository keeps finding.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import pytest
from sqlalchemy import text

from app.api import security as sec


@pytest.fixture()
def client(db):
    from fastapi.testclient import TestClient

    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def _as(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


OWNER, ANALYST, OWNER_B = "USR_A_OWNER", "USR_A_ANALYST", "USR_B_OWNER"


def _control(body: dict, key: str) -> dict:
    return next(c for c in body["controls"] if c["key"] == key)


# --------------------------------------------------------------------------
# §41 — policy, including the parts a merchant cannot change
# --------------------------------------------------------------------------
def test_policy_lists_the_controls_a_merchant_cannot_change(client):
    """A page showing only the editable third invites the reader to believe
    that is all of policy."""
    body = client.get("/policy", headers=_as(OWNER)).json()
    keys = {c["key"] for c in body["controls"]}
    assert "refund_limit_minor" in keys

    assert _control(body, "dual_approval")["editable"] is False
    assert _control(body, "computed_risk")["editable"] is False
    for key in ("dual_approval", "computed_risk", "approval_ttl_seconds"):
        assert len(_control(body, key)["why"]) > 40, f"{key} is listed without a reason"


def test_policy_names_the_key_that_is_stored_and_does_nothing(client):
    """`auto_approve_below_minor` is written to every merchant by the seeder and
    read by no code path. Somebody setting it would believe small refunds
    auto-approve; nothing would happen.

    Surfaced rather than omitted, because omitting it is how it stayed
    invisible.
    """
    body = client.get("/policy", headers=_as(OWNER)).json()
    dead = _control(body, "auto_approve_below_minor")
    assert dead["editable"] is False
    assert "NOT IMPLEMENTED" in dead["why"]


def test_an_override_reads_as_an_override(client):
    """The effective value alone cannot distinguish "chosen" from "inherited"."""
    kettle = client.get("/policy", headers=_as(OWNER)).json()
    assert _control(kettle, "refund_limit_minor")["overridden"] is True

    # MERCH_B is seeded at 200000 against a 500000 default, so effective and
    # default genuinely differ there.
    northwind = _control(client.get("/policy", headers=_as(OWNER_B)).json(),
                         "refund_limit_minor")
    assert northwind["effective"] == 200000
    assert northwind["default"] != northwind["effective"]


def test_only_an_owner_may_change_policy(client):
    r = client.put("/policy", headers=_as(ANALYST), json={"refund_limit_minor": 1})
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "role_required"


def test_changing_the_limit_takes_effect_and_is_recorded(client, db):
    r = client.put("/policy", headers=_as(OWNER), json={"refund_limit_minor": 250000})
    assert r.status_code == 200, r.text
    assert r.json()["changed"] is True

    assert _control(client.get("/policy", headers=_as(OWNER)).json(),
                    "refund_limit_minor")["effective"] == 250000

    # Raising or lowering what this system will move without a second human is
    # exactly the change that should be attributable afterwards.
    assert db.execute(text(
        "SELECT COUNT(*) FROM audit_logs WHERE event_type = 'policy_changed'")
    ).scalar_one() >= 1


def test_null_clears_the_override_rather_than_setting_zero(client):
    """Zero is a real limit that refuses every refund. A UI producing it by
    accident when somebody meant "back to default" would be a quiet outage."""
    client.put("/policy", headers=_as(OWNER), json={"refund_limit_minor": 250000})
    client.put("/policy", headers=_as(OWNER), json={"refund_limit_minor": None})

    control = _control(client.get("/policy", headers=_as(OWNER)).json(),
                       "refund_limit_minor")
    assert control["overridden"] is False
    assert control["effective"] == control["default"]


def test_a_negative_limit_is_refused(client):
    r = client.put("/policy", headers=_as(OWNER), json={"refund_limit_minor": -1})
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_limit"


def test_the_policy_engine_honours_a_changed_limit(client, db):
    """The assertion that makes the screen worth having: the number it writes is
    the number the engine reads."""
    from app.policy.risk import _merchant_refund_limit

    client.put("/policy", headers=_as(OWNER), json={"refund_limit_minor": 111100})
    assert _merchant_refund_limit(db, "MERCH_A") == 111100


# --------------------------------------------------------------------------
# §28 — the auditor's question, which is not about one object
# --------------------------------------------------------------------------
@pytest.fixture()
def trail(db):
    """Audit rows to search. The seed writes none."""
    now = datetime.now(UTC)
    rows = [
        ("user_created", "USR_A_OWNER", "MERCH_A", now - timedelta(days=1)),
        ("user_created", "USR_A_OWNER", "MERCH_A", now - timedelta(days=3)),
        ("policy_changed", "USR_A_APPROVER", "MERCH_A", now - timedelta(days=2)),
        # Another merchant's activity, which must never appear.
        ("user_created", "USR_B_OWNER", "MERCH_B", now - timedelta(days=1)),
    ]
    for event, actor, merchant, at in rows:
        db.execute(text(
            "INSERT INTO audit_logs (event_type, user_id, merchant_id, payload, "
            "                        created_at) "
            "VALUES (:e, :u, :m, '{}', :at)"),
            {"e": event, "u": actor, "m": merchant, "at": at})
    db.flush()


def test_the_trail_is_searchable_and_scoped_to_the_merchant(client, trail):
    body = client.get("/audit", headers=_as(OWNER)).json()
    assert body["matched"] == 3, "another merchant's activity is visible"
    assert all(e["merchant_id"] == "MERCH_A" for e in body["entries"])


def test_it_filters_by_what_happened_and_by_who(client, trail):
    by_kind = client.get("/audit?event_type=user_created", headers=_as(OWNER)).json()
    assert by_kind["matched"] == 2

    by_actor = client.get("/audit?actor=USR_A_APPROVER", headers=_as(OWNER)).json()
    assert by_actor["matched"] == 1
    assert by_actor["entries"][0]["event_type"] == "policy_changed"


def test_it_filters_by_window(client, trail):
    # Encoded, because a bare `+00:00` decodes to a space in a query string.
    since = quote((datetime.now(UTC) - timedelta(days=2, hours=1)).isoformat())
    body = client.get(f"/audit?since={since}", headers=_as(OWNER)).json()
    assert body["matched"] == 2


def test_the_count_is_of_the_filter_not_of_the_page(client, trail):
    """Deriving a total from a LIMITed query is a defect this repository has
    written three times."""
    body = client.get("/audit?limit=1", headers=_as(OWNER)).json()
    assert len(body["entries"]) == 1
    assert body["matched"] == 3


def test_paging_walks_backwards_without_skipping_or_repeating(client, trail):
    first = client.get("/audit?limit=2", headers=_as(OWNER)).json()
    assert len(first["entries"]) == 2
    assert first["next_cursor"] is not None

    second = client.get(f"/audit?limit=2&before_id={first['next_cursor']}",
                        headers=_as(OWNER)).json()
    seen = [e["id"] for e in first["entries"] + second["entries"]]
    assert len(seen) == len(set(seen)), "a row appeared on two pages"
    # Keyset, not OFFSET: an append-only table receiving writes mid-scroll must
    # not shift the window under the reader.
    assert seen == sorted(seen, reverse=True)


def test_the_last_page_says_it_is_the_last(client, trail):
    body = client.get("/audit?limit=50", headers=_as(OWNER)).json()
    assert body["next_cursor"] is None


def test_the_filter_offers_what_is_actually_there(client, trail):
    """Built from the data. A hardcoded list of event types goes stale in the
    direction that hides events."""
    body = client.get("/audit", headers=_as(OWNER)).json()
    assert set(body["event_types"]) == {"user_created", "policy_changed"}


# --------------------------------------------------------------------------
# §26 — how the provider has actually behaved
# --------------------------------------------------------------------------
@pytest.fixture()
def outcomes(db):
    """Provider outcomes to summarise. The seed leaves `agent_actions` empty."""
    now = datetime.now(UTC)
    # `agent_actions.task_id` is NOT NULL: an action exists because an
    # investigation proposed it.
    db.execute(text(
        "INSERT INTO agent_tasks (id, merchant_id, user_id, request, status, "
        "                         findings, agent_version, is_replay, "
        "                         model_requires_human, tool_call_count, "
        "                         llm_turn_count, attempts, created_at) "
        "VALUES ('TASK_H', 'MERCH_A', 'USR_A_OWNER', 'health fixture', "
        "        'COMPLETED', '[]', 'v1', false, false, 0, 0, 0, now())"))
    rows = [
        ("refund", "SUCCESS", 120, now - timedelta(days=1)),
        ("refund", "SUCCESS", 300, now - timedelta(days=1)),
        ("refund", "FAILED", 90, now - timedelta(days=2)),
        ("refund", "UNKNOWN", 8000, now - timedelta(days=2)),
        ("payment_link", "SUCCESS", 60, now - timedelta(days=3)),
    ]
    for i, (kind, state, latency, at) in enumerate(rows):
        db.execute(text(
            "INSERT INTO agent_actions (id, task_id, merchant_id, action_type, "
            "                           target_payment_id, amount_minor, status, "
            "                           verification_state, provider_latency_ms, "
            "                           idempotency_key, verify_attempts, escalated, "
            "                           created_at, updated_at) "
            "VALUES (:i, 'TASK_H', 'MERCH_A', :k, 'SYN_PAY_0001', 1000, "
            "        'COMPLETED', :s, :l, :i, 0, false, :at, :at)"),
            {"i": f"ACT_H{i}", "k": kind, "s": state, "l": latency, "at": at})
    db.flush()


def test_provider_health_breaks_outcomes_down_by_operation(client, outcomes):
    body = client.get("/provider-health", headers=_as(OWNER)).json()
    refunds = next(o for o in body["operations"] if o["action_type"] == "refund")

    assert refunds["attempted"] == 4
    assert refunds["succeeded"] == 2
    assert refunds["failed"] == 1
    # The column that must not be folded into either of the others.
    assert refunds["unknown"] == 1


def test_unknown_is_never_counted_as_a_failure(client, outcomes):
    """A success rate of `succeeded / attempted` quietly calls an unestablished
    outcome a failure. Three columns, and the reader decides."""
    refunds = next(o for o in client.get("/provider-health", headers=_as(OWNER))
                   .json()["operations"] if o["action_type"] == "refund")
    assert refunds["succeeded"] + refunds["failed"] + refunds["unknown"] == \
        refunds["attempted"]


def test_it_reports_provider_latency(client, outcomes):
    refunds = next(o for o in client.get("/provider-health", headers=_as(OWNER))
                   .json()["operations"] if o["action_type"] == "refund")
    assert refunds["p50_latency_ms"] is not None
    # The slow UNKNOWN is in the tail, which is the point of showing p95.
    assert refunds["p95_latency_ms"] >= refunds["p50_latency_ms"]


def test_it_gives_a_history_rather_than_only_a_verdict(client, outcomes):
    """`/readiness` answers "is it reachable now". An operator deciding whether
    to keep acting needs to know whether now is unusual."""
    body = client.get("/provider-health", headers=_as(OWNER)).json()
    assert len(body["history"]) >= 2
    assert sum(d["attempted"] for d in body["history"]) == 5


def test_it_carries_the_current_verdict_too(client, outcomes):
    body = client.get("/provider-health", headers=_as(OWNER)).json()
    assert body["status"] == "healthy"
    assert body["mode"] == "mock"
    # Stated plainly: a mock adapter is not a provider that answered.
    assert body["execution_is_real"] is False


def test_the_window_is_bounded(client, outcomes):
    """One day excludes the three-day-old payment link, which is what makes the
    parameter real rather than decorative."""
    body = client.get("/provider-health?days=1", headers=_as(OWNER)).json()
    kinds = {o["action_type"] for o in body["operations"]}
    assert "payment_link" not in kinds


def test_a_mistyped_window_is_a_bad_request_not_a_broken_server(client, trail):
    """It was a 500: the raw string reached the driver and Postgres refused it.

    A filter somebody mistyped is a bad request, and the message says the thing
    that actually catches people out -- a `+` in an offset has to be encoded.
    """
    r = client.get("/audit?since=not-a-date", headers=_as(OWNER))
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "invalid_timestamp"
    assert "URL-encode" in r.json()["detail"]["error"]

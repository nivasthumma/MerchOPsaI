"""The operations console — plan P0-03, P0-04, P0-05, P1-03, P1-06, §11.

Each of these screens exists because assembling it in the browser would mean
several requests, several instants, and a client doing arithmetic on money. So
what is asserted here is the server-side shape: one read, correct sections,
merchant-scoped in SQL.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api import security as sec
from app.api.main import app


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def _unknown_action(db, owner):
    """An action that reached UNKNOWN honestly: the refund lands, the response
    is lost."""
    from app.agent.approval import approve_and_execute
    from app.agent.runtime import AgentRuntime
    from app.integrations.razorpay.faults import Fault, FaultInjector

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    r = approve_and_execute(db, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    return r["action"]


# ----------------------------------------------------------- Action Center
def test_the_action_center_partitions_the_queue(client, db, owner):
    """An action appears in exactly one section. A row in two places is a row
    somebody will action twice."""
    action = _unknown_action(db, owner)

    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    assert body["sections"] == ["awaiting_approval", "executing", "unknown",
                                "escalated", "recently_completed", "failed"]

    placements = [s for s in ("executing", "unknown", "escalated",
                              "recently_completed")
                  if action.id in [r["id"] for r in body[s]]]
    assert placements == ["unknown"], placements


def test_no_action_is_ever_in_two_sections(client, db, owner):
    """The partition, asserted over every state an action can reach rather than
    over the one the happy path produces.

    The case that broke it: an action escalated, then settled by a later
    re-verification. `escalated` stays true -- a human really was handed it --
    so it sat in the escalated queue *and* in recently-completed. An operator
    picking up finished work is bad anywhere; here it is bad in the one place
    where acting twice moves money twice.
    """
    from app.integrations.razorpay.adapter import get_adapter
    from app.tools.actions import reverify_action
    from app.verification.reconciler import escalate_exhausted

    action = _unknown_action(db, owner)

    def placements() -> list[str]:
        body = client.get("/actions", headers=token("USR_A_OWNER")).json()
        return [s for s in ("executing", "unknown", "escalated",
                            "recently_completed")
                if action.id in [r["id"] for r in body[s]]]

    assert placements() == ["unknown"]

    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :i"),
               {"i": action.id})
    db.expire(action)
    escalate_exhausted(db)
    db.flush()
    assert placements() == ["escalated"]

    # And now the provider answers. The action is escalated *and* SUCCESS.
    reverify_action(db, get_adapter(db), action)
    db.flush()
    assert action.escalated is True, "escalation is a historical fact, not a toggle"
    assert action.verification_state.value == "SUCCESS"
    assert placements() == ["recently_completed"]


def test_an_escalated_action_leaves_the_unknown_section(client, db, owner):
    """It is still UNKNOWN. Listing it under "we are working on it" is the
    precise misreport the escalated section exists to prevent."""
    from app.verification.reconciler import escalate_exhausted

    action = _unknown_action(db, owner)
    db.execute(text("UPDATE agent_actions SET verify_attempts = 5 WHERE id = :i"),
               {"i": action.id})
    db.expire(action)
    escalate_exhausted(db)
    db.flush()

    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    assert action.id not in [r["id"] for r in body["unknown"]]
    assert action.id in [r["id"] for r in body["escalated"]]
    assert body["counts"]["escalated"] == 1


def test_an_action_row_carries_what_the_plan_requires(client, db, owner):
    """P0-03: amount, payment, incident/task, risk, policy, approval, provider
    reference, verification state, attempts, age. A row that lists identifiers
    makes the queue a lookup exercise."""
    _unknown_action(db, owner)

    row = client.get("/actions", headers=token("USR_A_OWNER")).json()["unknown"][0]
    for field in ("amount_minor", "target_payment_id", "task_id", "customer_id",
                  "verification_state", "verify_attempts", "created_at",
                  "provider", "environment", "owner", "action_type"):
        assert row[field] is not None, f"{field} is missing from the queue row"
    assert row["next_verify_at"], "P0-04 requires a visible next retry"
    assert row["provider"] == "razorpay"
    assert row["environment"] == "test"


def test_a_pending_approval_is_listed_as_an_approval_not_an_action(client, db, owner):
    """No action row exists until the approval clears. Modelling it as an
    action with null everything would tell an operator an action exists."""
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    assert out.approval is not None
    db.flush()

    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    ids = [r["approval_id"] for r in body["awaiting_approval"]]
    assert out.approval.id in ids
    row = next(r for r in body["awaiting_approval"] if r["approval_id"] == out.approval.id)
    assert row["expired"] is False
    assert row["risk_level"]
    assert row["signatures"] == 0
    assert body["counts"]["awaiting_approval"] >= 1


def test_expiry_is_decided_by_the_database_clock(client, db, owner):
    """A browser deciding an approval has expired, from a timestamp and its own
    clock, can be wrong in the direction that matters."""
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    db.execute(text("UPDATE approvals SET expires_at = now() - interval '1 hour' "
                    "WHERE id = :a"), {"a": out.approval.id})
    db.flush()

    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    row = next(r for r in body["awaiting_approval"] if r["approval_id"] == out.approval.id)
    assert row["expired"] is True


def test_the_action_center_is_merchant_scoped(client, db, owner):
    _unknown_action(db, owner)
    body = client.get("/actions", headers=token("USR_B_OWNER")).json()
    assert body["merchant_id"] == "MERCH_B"
    assert body["counts"]["unknown"] == 0


def test_the_count_is_a_total_not_the_length_of_the_page(client, db, owner):
    """The defect this pins, in full.

    Every section is capped at `limit`, and the count used to be `len(page)`.
    `command_center` counts the same quantities in SQL, so with more unresolved
    actions than fit on a page the two screens disagreed — and the SMALLER
    number was on the Action Center, the page an operator acts from.
    """
    from sqlalchemy import text as sql

    action = _unknown_action(db, owner)
    # Clone the unsettled action past the page size. Cloned in SQL rather than
    # executed for real: what is under test is counting, not execution, and
    # eight more refunds would need eight more mapped payments.
    for i in range(8):
        db.execute(sql("""
            INSERT INTO agent_actions
                (id, task_id, merchant_id, action_type, target_payment_id,
                 external_payment_id, amount_minor, idempotency_key, status,
                 verification_state, verify_attempts, escalated,
                 created_at, updated_at)
            SELECT :id, task_id, merchant_id, action_type, target_payment_id,
                   external_payment_id, amount_minor, :key, status,
                   verification_state, verify_attempts, escalated,
                   created_at, updated_at
              FROM agent_actions WHERE id = :src
        """), {"id": f"ACT_CLONE{i:04d}", "key": f"clone-{i}", "src": action.id})
    db.flush()

    body = client.get("/actions?limit=3", headers=token("USR_A_OWNER")).json()

    assert len(body["unknown"]) == 3, "the page is capped"
    assert body["shown"]["unknown"] == 3
    assert body["counts"]["unknown"] == 9, "the count is the total, not the page"
    assert body["limit"] == 3

    # And the two screens now agree, which is the property that actually
    # matters: they are read minutes apart by the same person.
    cc = client.get("/command-center", headers=token("USR_A_OWNER")).json()
    assert cc["attention"]["unknown_actions"] == body["counts"]["unknown"]


def test_a_section_that_fits_reports_the_same_number_twice(client, db, owner):
    """The ordinary case must not have become subtler: when nothing is
    truncated, `counts` and `shown` agree and a client rendering either is
    right."""
    _unknown_action(db, owner)
    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    assert body["counts"] == body["shown"]


def test_the_reconciliation_policy_is_published_not_copied(client, db):
    """The UI renders "gives up after N attempts" from the system's own rule
    rather than a second copy of it."""
    from app.verification.schedule import MAX_ATTEMPTS

    body = client.get("/actions", headers=token("USR_A_OWNER")).json()
    assert body["reconciliation_policy"]["max_attempts"] == MAX_ATTEMPTS
    assert body["reconciliation_policy"]["on_exhaustion"] == "escalate"


# ---------------------------------------------------------- Command Center
def test_the_command_center_answers_what_needs_my_attention(client, db, owner):
    from app.agent.runtime import AgentRuntime

    AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    db.flush()

    body = client.get("/command-center", headers=token("USR_A_OWNER")).json()
    assert body["attention"]["approvals_pending"] >= 1
    for key in ("approvals_expired", "unknown_actions", "escalated_actions",
                "open_incidents", "critical_incidents", "running_tasks"):
        assert key in body["attention"]


def test_the_funnel_is_ordered_and_named_by_the_server(client, db):
    """P1-03: at risk must never read as recovered. The surest way to keep that
    true is never to hand a client six loose numbers to arrange."""
    body = client.get("/command-center", headers=token("USR_A_OWNER")).json()
    assert [s["stage"] for s in body["funnel"]] == [
        "AT_RISK", "RECOVERABLE", "ATTEMPTED", "RECOVERED"]
    assert all("amount_minor" in s and s["label"] for s in body["funnel"])


def test_the_funnel_never_widens_as_it_descends(client, db):
    """The invariant the ledger already asserts, checked where it is rendered.
    A funnel whose later stages exceed its earlier ones is a funnel claiming
    more was recovered than was ever at risk."""
    body = client.get("/command-center", headers=token("USR_A_OWNER")).json()
    amounts = [s["amount_minor"] for s in body["funnel"]]
    assert amounts == sorted(amounts, reverse=True), amounts
    assert body["revenue"]["invariants_broken"] == []


def test_the_command_center_is_merchant_scoped(client, db):
    a = client.get("/command-center", headers=token("USR_A_OWNER")).json()
    b = client.get("/command-center", headers=token("USR_B_OWNER")).json()
    assert a["merchant_id"] == "MERCH_A"
    assert b["merchant_id"] == "MERCH_B"
    assert all(e.get("merchant_id") != "MERCH_A" for e in b["activity"])


# --------------------------------------------------------------- search
def test_search_resolves_every_identifier_the_plan_names(client, db, owner):
    from app.agent.runtime import AgentRuntime

    out = AgentRuntime(db, owner).run(
        "Refund the duplicate payment SYN_PAY_0002 amount 499900.")
    db.flush()

    for q, kind in (("SYN_PAY_0002", "payment"),
                    (out.task.id, "task")):
        body = client.get(f"/search?q={q}", headers=token("USR_A_OWNER")).json()
        assert kind in [r["kind"] for r in body["results"]], (q, body)


def test_search_finds_a_payment_by_its_provider_reference(client, db):
    """The identifier an operator arrives with when they are looking at the
    provider's dashboard rather than ours."""
    external = db.execute(text(
        "SELECT external_payment_id FROM provider_mappings LIMIT 1")).scalar()
    body = client.get(f"/search?q={external}", headers=token("USR_A_OWNER")).json()
    assert "payment" in [r["kind"] for r in body["results"]]


def test_search_is_exact_and_never_partial(client, db):
    """A prefix search over payment ids invites acting on whichever row sorted
    first, and every identifier here is pasted rather than typed."""
    body = client.get("/search?q=SYN_PAY", headers=token("USR_A_OWNER")).json()
    assert body["results"] == []


def test_search_cannot_reach_another_merchant(client, db):
    """Scoped in SQL, not filtered afterwards. A filter applied after the fact
    is one that can be forgotten."""
    body = client.get("/search?q=SYN_PAY_0002", headers=token("USR_B_OWNER")).json()
    assert body["results"] == []


def test_an_empty_query_is_answered_not_scanned(client, db):
    body = client.get("/search?q=", headers=token("USR_A_OWNER")).json()
    assert body == {"query": "", "results": [], "truncated": False}


def test_search_requires_authentication(client, db):
    assert client.get("/search?q=SYN_PAY_0002").status_code == 401


# -------------------------------------------------------- liveness/readiness
def test_liveness_touches_nothing(client):
    """A liveness probe that touches the database restarts the API when the
    database blips, which is the one response guaranteed not to help."""
    r = client.get("/liveness")
    assert r.status_code == 200
    assert r.json()["status"] == "alive"


def test_readiness_reports_every_component(client, db):
    r = client.get("/readiness")
    assert r.status_code == 200
    body = r.json()
    assert set(body["components"]) == {
        "database", "payment_provider", "llm", "webhook_ingestion",
        "reconciliation", "mapping_integrity"}
    assert body["status"] in ("ready", "degraded")
    assert body["blocking"] == []


def test_only_a_required_component_can_block_readiness(client, db):
    """A stopped cron sweep must not take the API out of rotation: removing the
    one interface an operator has for finding out about the stopped sweep is
    not a fix."""
    body = client.get("/readiness").json()
    assert body["components"]["database"]["required"] is True
    for name in ("llm", "webhook_ingestion", "reconciliation", "payment_provider"):
        assert body["components"][name]["required"] is False


def test_an_unconfigured_component_is_not_a_failure(client, db):
    """No webhook secret is a posture. Reporting it as `down` teaches people to
    ignore the endpoint."""
    body = client.get("/readiness").json()
    assert body["components"]["llm"]["status"] == "not_configured"
    assert body["status"] != "not_ready"


def test_readiness_notices_a_mapping_disagreement(client, db):
    """Two authorities on one fact, kept deliberately and therefore checked."""
    db.execute(text("UPDATE provider_mappings SET external_payment_id = 'pay_DRIFTED0001' "
                    "WHERE payment_id = 'SYN_PAY_0001'"))
    db.flush()

    body = client.get("/readiness", headers=token("USR_A_OWNER")).json()
    assert body["components"]["mapping_integrity"]["status"] == "degraded"
    assert "mapping_integrity" in body["degraded"]
    # Degraded, not down: an operator can still read every screen, and the one
    # thing they must not do -- execute against a drifted mapping -- is refused
    # by the resolver rather than by taking the API offline.
    assert body["status"] == "degraded"
    assert body["blocking"] == []


def test_readiness_publishes_mapping_coverage_to_an_authenticated_caller(client, db):
    """"Seventeen refunds were rejected" is ambiguous between a broken mapping
    layer and a working one applied to unmapped data."""
    provider = client.get("/readiness", headers=token("USR_A_OWNER")) \
        .json()["components"]["payment_provider"]
    assert provider["mapped"] > 0
    assert provider["payments"] > provider["mapped"]
    assert provider["execution_is_real"] is False


def test_an_unauthenticated_probe_gets_verdicts_and_no_counts(client, db):
    """A probe cannot hold a token, so it gets the verdict. It does not get the
    row counts -- those are the same shape of fact `/metrics/prometheus` already
    refuses to serve without a scrape token, and drawing the line somewhere else
    here would be an inconsistency an attacker gets to choose between.
    """
    body = client.get("/readiness").json()

    assert body["status"] in ("ready", "degraded")
    for name, c in body["components"].items():
        assert set(c) == {"status", "detail", "required", "latency_ms"}, name

    blob = repr(body)
    # The seeded payment count and the mapped count must not be recoverable
    # from the public body, including out of a detail string.
    assert "590" not in blob and "\"payments\"" not in blob
    assert "mapped" not in blob


def test_an_authenticated_caller_gets_the_operational_detail(client, db):
    body = client.get("/readiness", headers=token("USR_A_OWNER")).json()
    assert body["components"]["payment_provider"]["payments"] > 0
    assert "unsettled" in body["components"]["reconciliation"]


def test_an_invalid_token_narrows_the_body_rather_than_failing_the_probe(client, db):
    """A probe presenting a stale token must not start failing the deployment's
    health check."""
    r = client.get("/readiness", headers={"Authorization": "Bearer nonsense.0000"})
    assert r.status_code == 200
    assert set(r.json()["components"]["database"]) == {
        "status", "detail", "required", "latency_ms"}


def test_a_drifted_mapping_names_the_rows_only_to_an_authenticated_caller(client, db):
    db.execute(text("UPDATE provider_mappings SET external_payment_id = 'pay_DRIFTED0002' "
                    "WHERE payment_id = 'SYN_PAY_0003'"))
    db.flush()

    public = client.get("/readiness").json()["components"]["mapping_integrity"]
    assert public["status"] == "degraded"
    assert "sample" not in public and "SYN_PAY_0003" not in repr(public)

    private = client.get("/readiness", headers=token("USR_A_OWNER")) \
        .json()["components"]["mapping_integrity"]
    assert private["drifted"] == 1
    assert private["sample"][0]["payment_id"] == "SYN_PAY_0003"

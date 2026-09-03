"""Concurrency tests — the failures that only exist when two things run at once.

Every other test in this suite runs on one connection inside one transaction.
That is the right default: it is fast, it isolates, and it rolls back. It also
cannot observe a race, because a race needs two connections committing against
each other, and the whole point of the fixture is that there is only ever one.

So these tests manage their own connections and clean up after themselves. They
are slower and noisier than the rest of the suite on purpose — this is the class
of defect that a green single-threaded suite is structurally blind to, and the
double-refund race sat behind exactly that blind spot.
"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.agent.approval import approve_and_execute
from app.agent.runtime import Principal
from app.db import get_engine
from app.models import (
    ActionStatus, AgentAction, AgentTask, Approval, TaskStatus,
)

# A payment at MERCH_A that carries an external mapping, so a refund can
# actually reach the (mock) adapter rather than being refused as unmapped.
MAPPED_PAYMENT_SQL = """
    SELECT id, amount_minor FROM payments
    WHERE merchant_id = 'MERCH_A' AND external_payment_id IS NOT NULL
      AND status = 'captured'
    ORDER BY id LIMIT 1
"""


@pytest.fixture
def committed_session(_seeded_schema):
    """A session that really commits, and undoes every trace of it afterwards.

    The `db` fixture cannot be used here: it pins the application's session
    factory to one connection so nothing escapes its rollback, which is exactly
    the behaviour a concurrency test has to avoid.

    Committing for real means this test owns its own cleanup, and cleanup has to
    reach further than the rows it inserted. A refund that executes also writes
    a `refunds` row and moves `payments.amount_refunded_minor` -- both committed,
    both shared with every other test in the run. Leaving them behind silently
    lowers the refundable balance on a seeded payment and breaks whichever test
    happens to use it next, which is a far worse failure than the one this file
    exists to catch: it lands somewhere else, under an unrelated name.

    So the payment's refund state is snapshotted going in and restored coming
    out. `audit_logs` is the one thing deliberately left alone -- it is
    append-only by trigger, and `task_id` on it is a plain column rather than a
    foreign key precisely so the trail outlives the task it describes.
    """
    engine = get_engine()
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    made: dict[str, list[str]] = {"tasks": [], "approvals": []}

    s = Session()
    before = {
        r["id"]: r["amount_refunded_minor"]
        for r in s.execute(text(
            "SELECT id, amount_refunded_minor FROM payments "
            "WHERE merchant_id = 'MERCH_A'")).mappings()
    }
    refunds_before = {
        r[0] for r in s.execute(text("SELECT id FROM refunds")).all()
    }
    try:
        yield s, made
    finally:
        s.close()
        with Session() as cleanup:
            if made["tasks"]:
                cleanup.execute(
                    text("DELETE FROM agent_actions WHERE task_id = ANY(:t)"),
                    {"t": made["tasks"]})
                cleanup.execute(
                    text("DELETE FROM approval_signatures WHERE approval_id = ANY(:a)"),
                    {"a": made["approvals"] or [""]})
                cleanup.execute(
                    text("DELETE FROM approvals WHERE task_id = ANY(:t)"),
                    {"t": made["tasks"]})
                cleanup.execute(
                    text("DELETE FROM agent_messages WHERE task_id = ANY(:t)"),
                    {"t": made["tasks"]})
                cleanup.execute(
                    text("DELETE FROM tool_calls WHERE task_id = ANY(:t)"),
                    {"t": made["tasks"]})
                cleanup.execute(
                    text("DELETE FROM agent_tasks WHERE id = ANY(:t)"),
                    {"t": made["tasks"]})

            # Anything the executed refund wrote to the shared business tables.
            cleanup.execute(text("DELETE FROM refunds WHERE NOT (id = ANY(:keep))"),
                            {"keep": list(refunds_before) or [""]})
            for pid, amount in before.items():
                cleanup.execute(
                    text("UPDATE payments SET amount_refunded_minor = :a "
                         "WHERE id = :p AND amount_refunded_minor <> :a"),
                    {"a": amount, "p": pid})
            cleanup.commit()


def _pending_refund_task(session, made, payment_id: str, amount_minor: int):
    """A task sitting at AWAITING_APPROVAL with one approved-pending refund."""
    tid = f"TASK_CONC_{uuid.uuid4().hex[:8].upper()}"
    aid = f"APR_CONC_{uuid.uuid4().hex[:8].upper()}"
    session.add(AgentTask(
        id=tid, merchant_id="MERCH_A", user_id="USR_A_OWNER",
        request="concurrency probe", status=TaskStatus.AWAITING_APPROVAL,
        agent_version="t", model_version="t", prompt_version="t",
    ))
    session.add(Approval(
        id=aid, task_id=tid, merchant_id="MERCH_A", action_type="request_refund",
        action_payload={"synthetic_payment_id": payment_id,
                        "amount_minor": amount_minor},
        evidence=[], risk_level="HIGH", decision="PENDING", required_signatures=1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
    ))
    session.commit()
    made["tasks"].append(tid)
    made["approvals"].append(aid)
    return tid, aid


def _bare_task(session) -> str:
    """A task row, because agent_actions.task_id is NOT NULL."""
    tid = f"TASK_CONC_{uuid.uuid4().hex[:8].upper()}"
    session.add(AgentTask(
        id=tid, merchant_id="MERCH_A", user_id="USR_A_OWNER",
        request="constraint probe", status=TaskStatus.AWAITING_APPROVAL,
        agent_version="t", model_version="t", prompt_version="t",
    ))
    session.flush()
    return tid


# ------------------------------------------------------------------ constraint
def test_a_second_live_refund_for_one_payment_cannot_be_inserted(db):
    """The guarantee, stated at the layer that actually enforces it.

    Two refund actions for one payment, both live, differing in every other
    column including the idempotency key — which is what two separate approvals
    produce. The database must refuse the second.
    """
    payment = db.execute(text(MAPPED_PAYMENT_SQL)).mappings().first()
    assert payment is not None, "seed carries no externally mapped captured payment"
    task_id = _bare_task(db)

    def action(suffix: str) -> AgentAction:
        return AgentAction(
            id=f"ACT_CONC_{suffix}", task_id=task_id, merchant_id="MERCH_A",
            action_type="refund", target_payment_id=payment["id"],
            external_payment_id="pay_probe", amount_minor=100,
            idempotency_key=f"key-{suffix}",   # deliberately DIFFERENT keys
            status=ActionStatus.PENDING, approval_id=f"APR_{suffix}",
        )

    db.add(action("A"))
    db.flush()

    db.add(action("B"))
    with pytest.raises(IntegrityError) as exc:
        db.flush()
    assert "uq_live_refund_per_payment" in str(exc.value)


def test_a_refund_may_follow_one_that_failed(db):
    """The constraint bounds live refunds, not the payment's whole history.

    A refund that failed leaves the payment refundable, and a second attempt has
    to be able to reserve. A constraint that forbade that would turn one
    provider error into a permanently unrefundable payment.
    """
    payment = db.execute(text(MAPPED_PAYMENT_SQL)).mappings().first()
    task_id = _bare_task(db)

    first = AgentAction(
        id="ACT_CONC_FAILED", task_id=task_id, merchant_id="MERCH_A",
        action_type="refund", target_payment_id=payment["id"],
        external_payment_id="pay_probe", amount_minor=100,
        idempotency_key="key-failed", status=ActionStatus.FAILED,
        approval_id="APR_1",
    )
    db.add(first)
    db.flush()

    second = AgentAction(
        id="ACT_CONC_RETRY", task_id=task_id, merchant_id="MERCH_A",
        action_type="refund", target_payment_id=payment["id"],
        external_payment_id="pay_probe", amount_minor=100,
        idempotency_key="key-retry", status=ActionStatus.PENDING,
        approval_id="APR_2",
    )
    db.add(second)
    db.flush()   # must not raise

    live = db.execute(text("""
        SELECT count(*) FROM agent_actions
        WHERE target_payment_id = :p AND action_type = 'refund'
          AND status IN ('PENDING','SUBMITTED','CONFIRMED')
    """), {"p": payment["id"]}).scalar()
    assert live == 1


def test_the_constraint_is_scoped_to_refunds(db):
    """A payment link and a refund for the same payment are not the same action."""
    payment = db.execute(text(MAPPED_PAYMENT_SQL)).mappings().first()
    task_id = _bare_task(db)

    db.add(AgentAction(
        id="ACT_CONC_REF", task_id=task_id, merchant_id="MERCH_A",
        action_type="refund", target_payment_id=payment["id"],
        external_payment_id="pay_probe", amount_minor=100,
        idempotency_key="key-ref", status=ActionStatus.PENDING, approval_id="APR_1"))
    db.add(AgentAction(
        id="ACT_CONC_LINK", task_id=task_id, merchant_id="MERCH_A",
        action_type="payment_link", target_payment_id=payment["id"],
        external_payment_id="pay_probe", amount_minor=100,
        idempotency_key="key-link", status=ActionStatus.PENDING, approval_id="APR_2"))
    db.flush()   # must not raise


# ------------------------------------------------------------------ real race
@pytest.mark.slow
def test_two_approvals_for_one_payment_produce_one_refund(committed_session, monkeypatch):
    """The end-to-end race, driven with two real connections.

    Two tasks, two approvals, one payment, approved from two threads.

    The interleaving is forced rather than hoped for. Left to the scheduler the
    threads simply take turns -- the first commits its reservation before the
    second runs its policy check, the check does its job, and the test passes
    without ever entering the window it exists to probe. A concurrency test that
    only sometimes reproduces the concurrency is worse than no test, because it
    reports green for the wrong reason.

    So both threads are held at a barrier placed *after* the policy re-check and
    *before* the reservation insert. That is exactly the gap the SELECT cannot
    hold, and it is the state the production race arrives at by luck. Whatever
    happens after the barrier is the real behaviour under contention.
    """
    session, made = committed_session
    payment = session.execute(text(MAPPED_PAYMENT_SQL)).mappings().first()
    assert payment is not None

    t1, _ = _pending_refund_task(session, made, payment["id"], 100)
    t2, _ = _pending_refund_task(session, made, payment["id"], 100)

    principal = Principal("TEN_KETTLE", "USR_A_OWNER", "MERCH_A", "owner",
                          ["read:metrics", "read:orders", "action:refund",
                           "action:recover"])

    # Both threads must be past policy and about to reserve before either
    # proceeds. `derive_idempotency_key` is the last call before the INSERT.
    import app.tools.actions as actions_mod
    window = threading.Barrier(2, timeout=15)
    real_derive = actions_mod.derive_idempotency_key

    def synchronised_derive(*a, **kw):
        key = real_derive(*a, **kw)
        window.wait()
        return key

    monkeypatch.setattr(actions_mod, "derive_idempotency_key", synchronised_derive)

    engine = get_engine()
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    results: dict[str, object] = {}

    def run(task_id: str) -> None:
        s = Session()
        try:
            approve_and_execute(s, task_id, principal)
            s.commit()
            results[task_id] = "executed"
        except Exception as exc:        # noqa: BLE001 - the refusal is the result
            s.rollback()
            results[task_id] = f"{type(exc).__name__}: {exc}"
        finally:
            s.close()

    threads = [threading.Thread(target=run, args=(t,)) for t in (t1, t2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=40)
    assert not any(t.is_alive() for t in threads), "a thread never finished"

    live = session.execute(text("""
        SELECT count(*) FROM agent_actions
        WHERE target_payment_id = :p AND action_type = 'refund'
          AND task_id = ANY(:t)
          AND status IN ('PENDING', 'SUBMITTED', 'CONFIRMED')
    """), {"p": payment["id"], "t": [t1, t2]}).scalar()

    assert live == 1, (
        f"expected exactly one live refund for {payment['id']}, found {live}. "
        f"Both approvals cleared policy in the same window and both reserved -- "
        f"that is the double refund. Thread outcomes: {results}"
    )


# ------------------------------------------------------------------ numeric range
def test_money_columns_hold_more_than_a_32_bit_ceiling(db):
    """Money is bigint, demonstrated by storing a value integer cannot hold.

    Asserting the declared type would only restate `app/models.py`. The property
    that matters is what the database accepts, so this writes a real figure past
    the 32-bit ceiling and reads it back. Under the old `integer` columns both
    of these raised `integer out of range`.

    ₹30 million is not a stress-test number. It is a mid-sized merchant having a
    bad afternoon -- which is exactly when revenue-at-risk is read.
    """
    over_32_bit = 3_000_000_000     # ₹30,000,000.00 in paise
    assert over_32_bit > 2_147_483_647

    # A per-payment amount, and an aggregate. The aggregate is the one that
    # would have overflowed first in practice.
    payment_id = db.execute(text(MAPPED_PAYMENT_SQL)).mappings().first()["id"]
    db.execute(text("UPDATE payments SET amount_minor = :a WHERE id = :p"),
               {"a": over_32_bit, "p": payment_id})
    assert db.execute(text("SELECT amount_minor FROM payments WHERE id = :p"),
                      {"p": payment_id}).scalar() == over_32_bit

    incident_id = db.execute(
        text("SELECT id FROM incidents LIMIT 1")).scalar()
    if incident_id is not None:
        db.execute(
            text("UPDATE incidents SET revenue_at_risk_minor = :a WHERE id = :i"),
            {"a": over_32_bit, "i": incident_id})
        assert db.execute(
            text("SELECT revenue_at_risk_minor FROM incidents WHERE id = :i"),
            {"i": incident_id}).scalar() == over_32_bit


def test_every_money_column_is_64_bit(db):
    """No monetary column may be left at 32 bits.

    Named columns get widened; the next one somebody adds is the risk. This
    reads the live catalogue rather than the models, so a column added as
    `Integer` fails here even if nobody remembers this file exists.
    """
    narrow = db.execute(text("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND (column_name LIKE '%_minor' OR column_name LIKE '%_amount')
          AND data_type <> 'bigint'
        ORDER BY table_name, column_name
    """)).mappings().all()

    assert not narrow, (
        "monetary columns still narrower than bigint: "
        + ", ".join(f"{r['table_name']}.{r['column_name']} ({r['data_type']})"
                    for r in narrow)
    )

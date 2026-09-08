"""Bring a database to a state where every console screen has something on it.

`scripts/seed_data.py` produces 590 payments and no *operations*: no incidents
until detection runs, no tasks, no approvals, no actions. So a freshly seeded
database opens on a Command Center reporting nothing to attend to and an Action
Center with five empty sections -- a console that is working correctly and looks
broken, which is the worst way for a demonstration to be wrong.

`scripts/run_e2e.sh` already knows how to build every interesting state,
including the one that cannot be produced over HTTP; it just does it for its own
database and throws it away afterwards. This is that knowledge, pointed at a
database somebody is going to look at.

    make demo-state

## What it makes, and why each one

    detection            incidents, so the Command Center has work on it
    a recovery plan      candidates, so the funnel is not four zeroes
    a gated task         Action Center -> Awaiting approval (P0-03)
    an executed refund   -> Recently completed, verified SUCCESS (§7)
    an UNKNOWN action    -> the reconciliation queue (P0-04), with attempts
                            and a next retry
    a rejected task      the path that makes ZERO external calls

## What it does not do

It does not seed, and it does not delete. Everything here is additive, so it is
safe to run against a database somebody is using -- which is the entire reason
it is a separate script from `seed_data.py` rather than a flag on it.

Re-running adds another round. That is deliberate: an operations console with
two of each is more honest than one that pretends every queue holds exactly one
item.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.agent.approval import approve_and_execute, reject
from app.agent.runtime import AgentRuntime, Principal
from app.db import session_scope
from app.detection.engine import detect
from app.integrations.razorpay.faults import Fault, FaultInjector

OWNER = Principal("TEN_KETTLE", "USR_A_OWNER", "MERCH_A", "owner",
                  ["read:metrics", "read:orders", "action:refund", "action:recover"])


def step(label: str) -> None:
    print(f"\n== {label}")


def _duplicate_payments(session, limit: int) -> list[str]:
    """Payments with a duplicate sibling, newest first.

    Read from the database rather than hardcoded. The seed is deterministic, so
    a literal id would work today and become a confusing failure the first time
    somebody changes the generator -- and the failure would be "the agent
    refused", several frames from the tuple that moved.
    """
    rows = session.execute(text("""
        SELECT p.id
          FROM payments p
          JOIN payments q
            ON q.order_id = p.order_id AND q.id <> p.id
             AND q.amount_minor = p.amount_minor
         WHERE p.merchant_id = 'MERCH_A' AND p.status = 'captured'
           AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.payment_id = p.id)
         ORDER BY p.created_at DESC
         LIMIT :n
    """), {"n": limit}).scalars().all()
    return list(rows)


def _amount(session, payment_id: str) -> int:
    return int(session.execute(
        text("SELECT amount_minor FROM payments WHERE id = :i"),
        {"i": payment_id}).scalar())


def main() -> int:
    step("detection")
    with session_scope() as s:
        report = detect(s, "MERCH_A")
        print(f"   {report.incidents_created} incident(s) created "
              f"({report.already_known} already known)")

    step("a recovery plan, so the funnel is not four zeroes")
    with session_scope() as s:
        from app.models import Incident, IncidentStatus
        from app.recovery.planner import plan_recovery
        open_incidents = (s.query(Incident)
                          .filter(Incident.merchant_id == "MERCH_A",
                                  Incident.status != IncidentStatus.RESOLVED)
                          .order_by(Incident.revenue_at_risk_minor.desc())
                          .limit(2).all())
        if not open_incidents:
            print("   no open incident to plan against; run detection first")
        for inc in open_incidents:
            res = plan_recovery(s, inc, principal=OWNER)
            print(f"   {inc.id}: plan {res.plan.id}, "
                  f"{len(res.candidates)} candidate(s), "
                  f"{res.plan.intervention.value}")

    with session_scope() as s:
        targets = _duplicate_payments(s, 4)
        amounts = {p: _amount(s, p) for p in targets}
    if len(targets) < 4:
        print(f"\nOnly {len(targets)} refundable duplicate(s) available; "
              f"the queues below will be shorter than usual.")

    def _run(payment_id: str, label: str):
        with session_scope() as s:
            out = AgentRuntime(s, OWNER).run(
                f"Refund the duplicate payment {payment_id} amount {amounts[payment_id]}.")
            print(f"   {label}: {out.task.id} -> {out.task.status.value}")
            return out.task.id, out.approval

    if targets:
        step("a task waiting on a human -- Action Center, Awaiting approval")
        _run(targets[0], "gated")

    if len(targets) > 1:
        step("an executed refund, verified against the provider")
        task_id, approval = _run(targets[1], "gated")
        if approval is not None:
            with session_scope() as s:
                r = approve_and_execute(s, task_id, OWNER)
                a = r["action"]
                print(f"   {a.id}: {a.verification_state.value}, "
                      f"external ref {a.external_reference}")

    if len(targets) > 2:
        step("one action whose outcome is genuinely unestablished")
        # Through the REAL execution path with the timeout injector, exactly as
        # `run_e2e.sh` plants its own: the refund lands, the response is lost,
        # and the action is left UNKNOWN the way it would be in production.
        # Writing UNKNOWN directly into the row would produce a queue entry that
        # never reached UNKNOWN the way the system does -- which is a screenshot,
        # not a state.
        task_id, approval = _run(targets[2], "gated")
        if approval is not None:
            with session_scope() as s:
                r = approve_and_execute(
                    s, task_id, OWNER,
                    injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
                a = r["action"]
                print(f"   {a.id}: {a.verification_state.value}, "
                      f"attempt {a.verify_attempts}, next check {a.next_verify_at}")

    if len(targets) > 3:
        step("a rejection -- the path that makes zero external calls")
        task_id, approval = _run(targets[3], "gated")
        if approval is not None:
            with session_scope() as s:
                t = reject(s, task_id, OWNER, reason="Not a duplicate on review.")
                # Counted with a query, not `len(task.actions)` -- there is no
                # such relationship, and the number is the point: a rejection
                # must leave no action row at all, because an action row is the
                # record of something having been sent.
                n = s.execute(text(
                    "SELECT count(*) FROM agent_actions WHERE task_id = :t"),
                    {"t": t.id}).scalar()
                print(f"   {t.id} -> {t.status.value}, actions recorded: {n}")

    print("\nDone. Open the console; every screen now has something on it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

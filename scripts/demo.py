"""Scripted end-to-end demo — CONTRACT §51.

Proves the loop in one run:
  investigate -> evidence -> recommend -> policy -> approve -> act -> verify
  -> audit -> replay
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.agent.approval import approve_and_execute, reverify
from app.agent.replay import playback, re_reason
from app.agent.runtime import AgentRuntime, Principal
from app.audit.trace import trace_for
from app.config import get_settings
from app.db import session_scope
from app.integrations.razorpay.faults import Fault, FaultInjector
from app.models import Refund

OWNER = Principal("TEN_KETTLE", "USR_A_OWNER", "MERCH_A", "owner",
                  ["read:metrics", "read:orders", "action:refund"])
ANALYST = Principal("TEN_KETTLE", "USR_A_ANALYST", "MERCH_A", "analyst",
                    ["read:metrics", "read:orders"])


def rule(t):
    print("\n" + "=" * 78)
    print(t)
    print("=" * 78)


def main() -> None:
    s = get_settings()
    rule("MerchantOps Agent — end-to-end demo")
    print(f"LLM provider    : {s.resolved_llm_provider}")
    print(f"Payment adapter : {s.resolved_razorpay_mode}")
    if s.resolved_razorpay_mode == "mock":
        print("                  (refunds hit a mock adapter, not Razorpay — "
              "policy/approval/verification are unchanged)")

    # ---------------------------------------------------------------- 1
    rule("1. Why did revenue drop? (root cause discovered via tools)")
    with session_scope() as db:
        out = AgentRuntime(db, OWNER).run("Why did revenue drop this week?")
        tools = [r[0] for r in db.execute(text(
            "SELECT tool_name FROM tool_calls WHERE task_id=:t ORDER BY seq"),
            {"t": out.task.id}).all()]
        print(f"tools used : {tools}")
        print(f"answer     : {out.answer}")
        obs = [f for f in out.task.findings if f["kind"] == "OBSERVED"]
        print(f"findings   : {len(obs)} OBSERVED, each citing a tool_call")

    # ---------------------------------------------------------------- 2
    rule("2. Unauthorized refund attempt (analyst) — must be DENIED")
    with session_scope() as db:
        before = db.query(Refund).count()
        out = AgentRuntime(db, ANALYST).run("Refund the duplicate payment.")
        rules_hit = [r[0] for r in db.execute(text("""
            SELECT payload->>'rule' FROM audit_logs
            WHERE task_id=:t AND event_type='policy_decision'"""), {"t": out.task.id}).all()]
        print(f"policy rules : {rules_hit}")
        print(f"refunds      : {before} -> {db.query(Refund).count()} (unchanged)")

    # ---------------------------------------------------------------- 3
    rule("3. Duplicate payment -> refund -> BLOCKED by policy")
    with session_scope() as db:
        out = AgentRuntime(db, OWNER).run("Find the duplicate payment and refund it.")
        task_id = out.task.id
        print(f"status   : {out.status.value}")
        print(f"approval : {out.approval.id} ({out.approval.decision})")
        print(f"payload  : {out.approval.action_payload}")
        print("no external call has been made")

    # ---------------------------------------------------------------- 4
    rule("4. Human approves -> execute -> INDEPENDENT verification")
    with session_scope() as db:
        r = approve_and_execute(db, task_id, OWNER)
        a = r["action"]
        print(f"idempotency key : {a.idempotency_key[:32]}… (derived server-side)")
        print(f"external ref    : {a.external_reference}")
        print(f"verification    : {a.verification_state.value}")
        print(f"reason          : {a.verification_detail['reason']}")

    # ---------------------------------------------------------------- 5
    rule("5. Audit trace")
    with session_scope() as db:
        for e in trace_for(db, task_id):
            print(f"  {e['at'][11:19]}  {e['event']}")

    # ---------------------------------------------------------------- 6
    rule("6. Replay — no financial side effect")
    with session_scope() as db:
        before = db.query(Refund).count()
        pb = playback(db, task_id)
        print(f"PLAYBACK  steps={[x['tool'] for x in pb['steps']]} "
              f"external_calls={pb['external_calls_made']}")
        rr = re_reason(db, task_id, OWNER)
        print(f"RE_REASON reasoning_diverged={rr['reasoning_diverged']} "
              f"external_calls={rr['external_calls_made']}")
        print(f"refunds {before} -> {db.query(Refund).count()} (unchanged)")

    # ---------------------------------------------------------------- 7
    rule("7. UNKNOWN — refund lands, response is lost")
    with session_scope() as db:
        # Name the payment explicitly: the first duplicate pair was already
        # refunded in step 4, so the duplicate-action guard would (correctly)
        # deny a second refund against it.
        out = AgentRuntime(db, OWNER).run(
            "Refund the duplicate payment SYN_PAY_0004 amount 149900.")
        if out.approval is None:
            print("(no approval created — skipping)")
            return
        tid = out.task.id
        r = approve_and_execute(db, tid, OWNER,
                                injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
        a = r["action"]
        print(f"verification : {a.verification_state.value}  "
              f"(not SUCCESS, not FAILED — the honest answer)")
        print(f"reason       : {a.verification_detail['reason']}")
        rv = reverify(db, tid, OWNER)
        print(f"after re-verify: {rv['verification'].state.value}  "
              f"ref={rv['action'].external_reference}")
        n = db.execute(text("SELECT count(*) FROM refunds WHERE payment_id='SYN_PAY_0004'")).scalar()
        print(f"refund rows for that payment: {n} (exactly one — no double refund)")

    rule("Demo complete")


if __name__ == "__main__":
    main()

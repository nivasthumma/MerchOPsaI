"""The v2 §20 states are reached by real work — ADR-0039.

A state machine whose states nobody transitions through is worse than a smaller
honest one: no scenario can grade it and a merchant reading a status list finds
half the entries unreachable. `EXECUTING` and `VERIFYING` were in exactly that
condition before this — present in the enum since v1 and entered by nothing.

So these tests run the real paths and assert the incident actually moved.
"""
from __future__ import annotations

import pytest

from app.agent.approval import approve_and_execute
from app.audit.trace import trace_for_incident
from app.detection import detect
from app.incidents.lifecycle import advance, transition, IllegalTransition
from app.incidents.manager import investigate
from app.models import Incident, IncidentStatus as S, IncidentType
from app.recovery import plan_recovery
from app.recovery.dispatch import dispatch_candidate, executable_candidates


def _moves(db, incident_id: str) -> list[tuple[str, str]]:
    return [(e["payload"]["from"], e["payload"]["to"])
            for e in trace_for_incident(db, incident_id)
            if e["event"] == "incident_status_changed"]


@pytest.fixture
def duplicate(db) -> Incident:
    detect(db, "MERCH_A")
    inc = (db.query(Incident)
           .filter(Incident.incident_type == IncidentType.DUPLICATE_PAYMENT)
           .first())
    assert inc is not None
    return inc


# ------------------------------------------------- the investigation phases
def test_investigation_passes_through_the_phases_it_actually_performs(db, duplicate, owner):
    investigate(db, duplicate, owner)
    visited = [to for _, to in _moves(db, duplicate.id)]

    assert S.EVIDENCE_COLLECTING.value in visited, "tools ran but no evidence phase"
    assert S.DIAGNOSING.value in visited, "an output block was parsed but nothing diagnosed"
    # And in order — evidence before diagnosis, not the other way round.
    assert (visited.index(S.EVIDENCE_COLLECTING.value)
            < visited.index(S.DIAGNOSING.value))


def test_the_phases_are_recorded_when_they_happen_not_at_the_end(db, duplicate, owner):
    """Back-dating would put every phase at the same instant as the outcome.

    The moves are separate audit rows in sequence, so a reader can see the run
    progressing rather than a block of states written together.
    """
    investigate(db, duplicate, owner)
    rows = [e for e in trace_for_incident(db, duplicate.id)
            if e["event"] == "incident_status_changed"]
    order = [e["payload"]["to"] for e in rows]
    assert order.index(S.EVIDENCE_COLLECTING.value) < order.index(
        S.ROOT_CAUSE_IDENTIFIED.value)
    # The evidence phase is recorded before the task's own completion event.
    events = [e["event"] for e in trace_for_incident(db, duplicate.id)]
    assert events.index("incident_status_changed") < events.index(
        "incident_investigated")


# ------------------------------------------------------ the execution tail
def test_the_whole_v2_chain_runs_end_to_end(db, duplicate, owner, approver):
    """§20's chain, driven by real work rather than asserted.

    Every move here is made by a different module: the runtime reports its
    phases, the planner records planning, dispatch records policy, and the
    approval path records approval, execution and verification.
    """
    investigate(db, duplicate, owner)
    plan = plan_recovery(db, duplicate, principal=owner).plan

    for c in executable_candidates(db, plan)[1:]:
        c.executable = False      # one candidate, so it is not bulk
    db.flush()

    out = dispatch_candidate(db, plan, executable_candidates(db, plan)[0], owner)
    approve_and_execute(db, out["task"].id, approver)
    db.refresh(duplicate)

    visited = [to for _, to in _moves(db, duplicate.id)]
    for expected in (S.EVIDENCE_COLLECTING, S.DIAGNOSING, S.ROOT_CAUSE_IDENTIFIED,
                     S.RECOVERY_PLANNED, S.POLICY_EVALUATING,
                     S.APPROVAL_REQUIRED, S.APPROVED, S.EXECUTING,
                     S.VERIFYING, S.MEASURING):
        assert expected.value in visited, f"{expected.value} was never entered"

    # Each state appears once: the chain advances, it does not oscillate.
    assert len(visited) == len(set(visited)), f"a state repeated: {visited}"


def test_an_undetermined_outcome_hands_the_incident_to_reconciliation(
        db, duplicate, owner, approver):
    """v2 §20's RECONCILING, on the path that actually produces it.

    A timeout AFTER submit is the case §53 exists for: the refund may or may not
    have happened. The incident must say somebody is looking, not that nobody
    could tell — and not that the work is done.

    Without this the whole UNKNOWN branch was untested: a mutation removing the
    RECONCILING move SURVIVED, because every other test here takes the SUCCESS
    path.
    """
    from app.integrations.razorpay.faults import Fault, FaultInjector

    investigate(db, duplicate, owner)
    plan = plan_recovery(db, duplicate, principal=owner).plan
    for c in executable_candidates(db, plan)[1:]:
        c.executable = False
    db.flush()

    out = dispatch_candidate(db, plan, executable_candidates(db, plan)[0], owner)
    approve_and_execute(db, out["task"].id, approver,
                        injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    db.refresh(duplicate)

    visited = [to for _, to in _moves(db, duplicate.id)]
    assert S.RECONCILING.value in visited, (
        f"an undetermined outcome did not reach reconciliation: {visited}")
    # And it did NOT claim the outcome was measured.
    assert S.MEASURING.value not in visited
    assert duplicate.status is S.RECONCILING


def test_execution_and_verification_are_no_longer_dead_states(db, duplicate, owner, approver):
    """The pre-existing gap this work closed.

    `EXECUTING` and `VERIFYING` shipped in v1's enum and nothing ever entered
    them; the execution path moved actions and left the incident behind.
    """
    investigate(db, duplicate, owner)
    plan = plan_recovery(db, duplicate, principal=owner).plan
    for c in executable_candidates(db, plan)[1:]:
        c.executable = False
    db.flush()
    out = dispatch_candidate(db, plan, executable_candidates(db, plan)[0], owner)
    approve_and_execute(db, out["task"].id, approver)

    visited = {to for _, to in _moves(db, duplicate.id)}
    assert {S.EXECUTING.value, S.VERIFYING.value} <= visited


# ------------------------------------------- advance vs transition
def test_advance_shrugs_where_transition_refuses(db, duplicate):
    """The asymmetry, asserted rather than described.

    `transition` guards the machine and refuses loudly. `advance` is called
    from paths where a provider has already been contacted, and must not raise
    back through one that has spent money.
    """
    assert duplicate.status is S.DETECTED

    with pytest.raises(IllegalTransition):
        transition(db, duplicate, S.MEASURING, reason="illegal")

    # Same illegal move, tolerated, and the incident is left where it was.
    advance(db, duplicate, S.MEASURING, reason="tolerated")
    assert duplicate.status is S.DETECTED


def test_advance_on_a_task_with_no_incident_is_not_an_error(db):
    """A merchant question is not an investigation. There is nothing to move."""
    class _Task:
        incident_id = None

    assert advance(db, _Task(), S.EXECUTING, reason="no incident") is None
    assert advance(db, None, S.EXECUTING, reason="nothing at all") is None


def test_advance_is_a_no_op_when_already_in_the_target_state(db, duplicate):
    advance(db, duplicate, S.TRIAGED, reason="first")
    before = len(_moves(db, duplicate.id))
    advance(db, duplicate, S.TRIAGED, reason="again")
    assert len(_moves(db, duplicate.id)) == before


def test_a_closed_incident_is_not_dragged_forward_by_a_late_action(db, duplicate, owner):
    """Somebody closing an incident underneath a running action must not be
    undone by the action finishing."""
    investigate(db, duplicate, owner)
    transition(db, duplicate, S.RESOLVED, reason="closed by hand", actor="USR_A_OWNER")
    transition(db, duplicate, S.CLOSED, reason="closed by hand", actor="USR_A_OWNER")

    advance(db, duplicate, S.EXECUTING, reason="a late provider call")
    assert duplicate.status is S.CLOSED

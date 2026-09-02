"""Incident lifecycle legality — MerchantOps §13."""
from __future__ import annotations

import pytest

from app.incidents.lifecycle import (
    EXCEPTION, IllegalTransition, is_legal, legal_from,
)
from app.models import IncidentStatus as S


def test_canonical_chain_is_walkable():
    chain = [S.DETECTED, S.TRIAGED, S.INVESTIGATING, S.ROOT_CAUSE_IDENTIFIED,
             S.RECOVERY_PLANNED, S.POLICY_EVALUATING, S.APPROVAL_REQUIRED,
             S.EXECUTING, S.VERIFYING, S.RESOLVED, S.CLOSED]
    for frm, to in zip(chain, chain[1:]):
        assert is_legal(frm, to), f"{frm.value} -> {to.value} should be legal"


def test_no_backward_movement():
    assert not is_legal(S.RESOLVED, S.DETECTED)
    assert not is_legal(S.EXECUTING, S.INVESTIGATING)
    assert not is_legal(S.INVESTIGATING, S.DETECTED)


def test_closed_is_terminal():
    assert legal_from(S.CLOSED) == set()


def test_every_live_state_can_fail_escalate_or_cancel():
    live = [S.DETECTED, S.TRIAGED, S.INVESTIGATING, S.ROOT_CAUSE_IDENTIFIED,
            S.RECOVERY_PLANNED, S.POLICY_EVALUATING, S.APPROVAL_REQUIRED,
            S.EXECUTING, S.VERIFYING]
    for state in live:
        assert EXCEPTION <= legal_from(state), f"{state.value} cannot reach an exception state"


def test_unknown_is_not_a_dead_end():
    """The incident-level mirror of the action-level UNKNOWN exit path. An
    incident that could never leave UNKNOWN would turn an unsettled state into
    a permanent one."""
    assert is_legal(S.UNKNOWN, S.RESOLVED)
    assert is_legal(S.UNKNOWN, S.VERIFYING)


def test_skipping_forward_is_legal_but_only_forward():
    # An incident needing no recovery resolves straight out of investigation.
    assert is_legal(S.INVESTIGATING, S.RESOLVED)
    # It cannot jump into the middle of execution without policy.
    assert not is_legal(S.INVESTIGATING, S.EXECUTING)


def test_transition_refuses_and_says_what_was_legal(db):
    from app.incidents.lifecycle import transition
    from app.models import Incident, IncidentSeverity, IncidentType
    from datetime import datetime, timezone

    inc = Incident(
        id="INC_TEST01", merchant_id="MERCH_A",
        incident_type=IncidentType.PAYMENT_DEGRADATION,
        severity=IncidentSeverity.HIGH, status=S.RESOLVED,
        title="t", summary="s", detection_key="k1",
        detection_rule="r", detection_version="v", correlation_id="c",
        started_at=datetime.now(timezone.utc),
    )
    db.add(inc)
    db.flush()

    with pytest.raises(IllegalTransition) as e:
        transition(db, inc, S.DETECTED, reason="should not happen")
    assert "cannot move RESOLVED -> DETECTED" in str(e.value)
    assert inc.status is S.RESOLVED          # unchanged


# ----------------------------------------------- v2 §20's added states
def test_the_full_v2_chain_is_walkable():
    """Every state v2 §20 names that this build reaches, in order.

    The v1 chain above still passes because each new state is inserted as a
    skippable step rather than a mandatory one — an incident that never
    collected evidence must still be able to conclude.
    """
    chain = [S.DETECTED, S.TRIAGED, S.INVESTIGATING, S.EVIDENCE_COLLECTING,
             S.DIAGNOSING, S.ROOT_CAUSE_IDENTIFIED, S.RECOVERY_PLANNED,
             S.POLICY_EVALUATING, S.APPROVAL_REQUIRED, S.APPROVED,
             S.EXECUTING, S.VERIFYING, S.MEASURING, S.RESOLVED, S.CLOSED]
    for frm, to in zip(chain, chain[1:]):
        assert is_legal(frm, to), f"{frm.value} -> {to.value} should be legal"


def test_the_new_phases_are_skippable():
    """A run that made no tool calls never collected evidence, and one that
    produced no output never diagnosed. Requiring the full chain would strand
    exactly the runs that did least."""
    assert is_legal(S.INVESTIGATING, S.ROOT_CAUSE_IDENTIFIED)
    assert is_legal(S.INVESTIGATING, S.DIAGNOSING)
    assert is_legal(S.EVIDENCE_COLLECTING, S.ROOT_CAUSE_IDENTIFIED)


def test_execution_can_proceed_without_an_approval_step():
    """Policy that returns ALLOW never asks anyone. An incident that needed no
    approval must not have to pretend it got one."""
    assert is_legal(S.POLICY_EVALUATING, S.EXECUTING)
    assert is_legal(S.APPROVAL_REQUIRED, S.EXECUTING)


def test_reconciling_is_reachable_from_unknown_and_says_someone_is_looking():
    """UNKNOWN says nobody could tell; RECONCILING says somebody is looking.
    An incident stuck in UNKNOWN with no way into the sweep would turn an
    unsettled state into a permanent one (§53)."""
    assert is_legal(S.UNKNOWN, S.RECONCILING)
    assert is_legal(S.VERIFYING, S.RECONCILING)
    assert is_legal(S.RECONCILING, S.MEASURING)
    assert is_legal(S.RECONCILING, S.RESOLVED)


def test_measuring_sits_between_acting_and_resolving():
    """RESOLVED used to claim both "we stopped acting" and "we know what it
    came to". Those are different facts and §49 keeps them apart."""
    assert is_legal(S.VERIFYING, S.MEASURING)
    assert is_legal(S.MEASURING, S.RESOLVED)
    # And measuring is not a way back into the work.
    assert not is_legal(S.MEASURING, S.EXECUTING)


def test_every_state_in_the_enum_is_reachable_or_a_start():
    """A state nothing can enter is a state no scenario can grade and no
    merchant will ever see. DETECTED is the only legal starting point."""
    reachable = {S.DETECTED}
    for frm in S:
        reachable |= legal_from(frm)
    orphans = [s.value for s in S if s not in reachable]
    assert orphans == [], f"unreachable states: {orphans}"

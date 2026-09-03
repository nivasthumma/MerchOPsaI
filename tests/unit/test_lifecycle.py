"""Incident lifecycle legality — MerchantOps §13."""
from __future__ import annotations

from datetime import UTC

import pytest

from app.incidents.lifecycle import (
    EXCEPTION,
    IllegalTransition,
    is_legal,
    legal_from,
)
from app.models import IncidentStatus as S


def test_canonical_chain_is_walkable():
    chain = [S.DETECTED, S.TRIAGED, S.INVESTIGATING, S.ROOT_CAUSE_IDENTIFIED,
             S.RECOVERY_PLANNED, S.POLICY_EVALUATING, S.APPROVAL_REQUIRED,
             S.EXECUTING, S.VERIFYING, S.RESOLVED, S.CLOSED]
    for frm, to in zip(chain, chain[1:], strict=False):
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
        assert legal_from(state) >= EXCEPTION, f"{state.value} cannot reach an exception state"


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
    from datetime import datetime

    from app.incidents.lifecycle import transition
    from app.models import Incident, IncidentSeverity, IncidentType

    inc = Incident(
        id="INC_TEST01", merchant_id="MERCH_A",
        incident_type=IncidentType.PAYMENT_DEGRADATION,
        severity=IncidentSeverity.HIGH, status=S.RESOLVED,
        title="t", summary="s", detection_key="k1",
        detection_rule="r", detection_version="v", correlation_id="c",
        started_at=datetime.now(UTC),
    )
    db.add(inc)
    db.flush()

    with pytest.raises(IllegalTransition) as e:
        transition(db, inc, S.DETECTED, reason="should not happen")
    assert "cannot move RESOLVED -> DETECTED" in str(e.value)
    assert inc.status is S.RESOLVED          # unchanged

"""Failure taxonomy, versioning and traces — MerchantOps §41, §47, §56, §57, §58."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime
from app.audit.trace import CANONICAL_EVENT, canonical, trace_by_correlation
from app.detection import detect
from app.failures import (
    TAXONOMY, Retryability, Subsystem, classify, describe, may_retry,
)
from app.incidents.manager import investigate
from app.integrations.razorpay.faults import Fault, FaultInjector
from app.models import AuditLog, FailureCode, Incident, IncidentType


# ------------------------------------------------------------------ §56, §57
def test_every_failure_code_is_classified():
    """A code with no class is INTERNAL_ERROR and escalates, which is safe but
    uninformative. Adding one without classifying it should be caught here
    rather than discovered by an operator reading a useless failure."""
    unclassified = [c.value for c in FailureCode if c.value not in TAXONOMY]
    assert unclassified == []


def test_an_unknown_code_escalates_rather_than_retrying():
    """Silently treating an unrecognised failure as retryable is how a
    permanent error becomes an infinite loop."""
    cls = classify("SOMETHING_NEW")
    assert cls.category == "INTERNAL_ERROR"
    assert cls.retryability is Retryability.ESCALATE
    assert may_retry("SOMETHING_NEW") is False
    assert describe("SOMETHING_NEW")["is_classified"] is False


@pytest.mark.parametrize("code", [
    "AUTHORIZATION_DENIED", "POLICY_DENIED", "APPROVAL_REJECTED",
    "TOOL_INVALID_ARGUMENT", "TOOL_UNAVAILABLE",
])
def test_authority_and_input_failures_are_never_retried(code):
    """§57 names these. The answer will be identical and the attempt is noise."""
    assert classify(code).retryability is Retryability.NEVER
    assert may_retry(code) is False


def test_an_unknown_financial_state_is_reconciled_and_never_retried():
    """The most important row in the table. A blind retry of an action whose
    outcome is unknown is the single most dangerous thing this system could do."""
    cls = classify("EXTERNAL_STATE_UNKNOWN")
    assert cls.retryability is Retryability.RECONCILE
    assert cls.owning_subsystem is Subsystem.RECONCILIATION
    # RECONCILE is a read, not a retry, and `may_retry` must not blur them.
    assert may_retry("EXTERNAL_STATE_UNKNOWN") is False
    assert "never re-issue" in cls.recommended_next_action.lower()


def test_transient_failures_are_the_only_retryable_ones():
    retryable = {c for c in TAXONOMY if may_retry(c)}
    assert retryable == {"TOOL_TIMEOUT", "EXTERNAL_API_ERROR",
                         "MODEL_INVALID_OUTPUT", "RATE_LIMITED"}


def test_a_failed_task_reports_its_class_not_just_its_code(db, owner):
    """A code tells an operator what broke. It does not tell them whether
    trying again is sensible, which is the question they have."""
    out = AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    approve_and_execute(db, out.task.id, owner,
                        injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    db.refresh(out.task)

    f = describe(out.task.failure_code)
    assert f["error_code"] == "EXTERNAL_STATE_UNKNOWN"
    assert f["category"] == "UNKNOWN_EXTERNAL_STATE"
    assert f["retryability"] == "RECONCILE"
    assert f["owning_subsystem"] == "reconciliation_engine"
    assert f["recommended_next_action"]


# ---------------------------------------------------------------------- §41
def test_every_run_records_what_it_takes_to_reproduce_it(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    t = out.task
    for field in ("agent_version", "model_provider", "model_version",
                  "prompt_version", "tool_registry_version", "policy_version",
                  "workflow_version"):
        assert getattr(t, field), f"{field} was not recorded"


def test_the_registry_version_is_derived_from_the_registry(db, owner, monkeypatch):
    """A hand-kept constant stops being true the first time someone adds a tool
    and forgets it — and §41's purpose is reproducibility, which a stale
    version defeats silently."""
    from app.tools import registry as reg

    before = reg.registry_version()
    spec = reg.REGISTRY["get_order"]
    monkeypatch.setitem(reg.REGISTRY, "get_order",
                        spec.model_copy(update={"required_permissions": ["read:everything"]}))
    assert reg.registry_version() != before, "widening a permission did not change the version"


def test_a_description_change_does_not_change_the_registry_version(monkeypatch):
    """It hashes what changes behaviour, not prose."""
    from app.tools import registry as reg

    before = reg.registry_version()
    spec = reg.REGISTRY["get_order"]
    monkeypatch.setitem(reg.REGISTRY, "get_order",
                        spec.model_copy(update={"description": "Totally different words."}))
    assert reg.registry_version() == before


# ----------------------------------------------------------------- §47, §58
def test_every_event_of_a_run_shares_one_correlation_id(db, owner):
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    ids = {r.correlation_id for r in db.query(AuditLog)
           .filter(AuditLog.task_id == out.task.id).all()}
    assert len(ids) == 1 and None not in ids


def test_an_incident_and_the_task_it_dispatched_share_a_trace(db, owner):
    """§58's complete trace. Detection, the lifecycle moves and the
    investigation are one story, so they carry one id."""
    detect(db, "MERCH_A")
    inc = db.query(Incident).filter(
        Incident.incident_type == IncidentType.DUPLICATE_PAYMENT).first()
    investigate(db, inc, owner)

    events = trace_by_correlation(db, inc.correlation_id, "MERCH_A")
    kinds = [e["event"] for e in events]
    assert "incident_detected" in kinds
    assert "task_created" in kinds
    assert "incident_status_changed" in kinds
    assert any(e["task_id"] for e in events)
    # Ordered, because a trace that is not ordered is a list.
    assert [e["id"] for e in events] == sorted(e["id"] for e in events)


def test_a_trace_is_merchant_scoped(db, owner):
    detect(db, "MERCH_A")
    inc = db.query(Incident).first()
    assert trace_by_correlation(db, inc.correlation_id, "MERCH_A")
    assert trace_by_correlation(db, inc.correlation_id, "MERCH_B") == []


def test_events_publish_the_spec_name_alongside_our_own(db, owner):
    """§47 uses its own vocabulary. Renaming ours would be a large diff whose
    only effect is to change strings, so both are published."""
    out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    from app.audit.trace import trace_for

    events = trace_for(db, out.task.id)
    named = {e["event"]: e["canonical_event"] for e in events}
    assert named["task_created"] == "TaskCreated"
    assert named["tool_call"] == "EvidenceCollected"
    assert named["policy_decision"] == "PolicyEvaluated"


def test_an_event_with_no_spec_name_keeps_its_own():
    """§47's list is explicitly 'Examples'. Inventing a canonical name for
    something it never mentions would claim a correspondence that is not there."""
    assert canonical("llm_turn") == "llm_turn"
    assert "llm_turn" not in CANONICAL_EVENT


def test_the_taxonomy_is_published(db, owner):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        body = c.get("/failures/taxonomy",
                     headers={"Authorization": f"Bearer {sec.issue_token('USR_A_OWNER')}"}).json()
    sec.reset_rate_limits()

    codes = {f["error_code"] for f in body["failures"]}
    assert "EXTERNAL_STATE_UNKNOWN" in codes
    unknown = next(f for f in body["failures"]
                   if f["error_code"] == "EXTERNAL_STATE_UNKNOWN")
    assert unknown["retryability"] == "RECONCILE"

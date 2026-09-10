"""The twenty mandatory adversarial scenarios, each mapped to evidence that exists.

The architecture review lists twenty adversarial cases the system must survive.
Most were already covered -- by a scenario in `data/scenarios/scenarios.yaml`, a
test, or both -- and the rest are covered by tests added with ADR-0053. This
module does not re-test them. It is the index: for every one of the twenty it
names where the proof lives, and it FAILS if any named proof does not exist.

That is the point of writing it as a test rather than a table in a document. A
table survives the deletion of the test it cites; this does not.

References are either `scenario:<ID>` (an evaluation scenario, run by
`make eval`) or `path::test_name` (a pytest test in this repository).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

MATRIX: dict[str, list[str]] = {
    "01 duplicate refund": [
        "scenario:SEC-03", "scenario:REF-22",
        "tests/unit/test_units.py::test_provider_replay_returns_same_refund",
        "tests/integration/test_flows.py::test_unique_idempotency_key_blocks_the_retry_the_balance_check_cannot",
        "tests/integration/test_remediation.py::test_a_changed_refund_under_a_reused_key_never_reaches_the_provider",
    ],
    "02 expired approval": [
        "scenario:REF-13",
        "tests/unit/test_units.py::test_expired_approval_is_invalid",
    ],
    "03 revoked approval": [
        "tests/integration/test_remediation.py::test_a_revoked_approval_never_executes",
        "tests/integration/test_remediation.py::test_revoke_is_an_endpoint",
    ],
    "04 wrong tenant": [
        "scenario:TEN-01", "scenario:TEN-02", "scenario:TEN-03",
        "tests/security/test_security.py::test_cross_tenant_order_read_denied",
        "tests/security/test_api_security.py::test_cross_merchant_task_is_not_visible",
        "tests/integration/test_tenancy.py::test_a_query_with_no_where_clause_sees_one_merchant",
        "tests/integration/test_remediation.py::test_the_database_scope_survives_a_mid_request_commit",
    ],
    "05 wrong payment mapping": [
        "scenario:REF-09",
        "tests/integration/test_provider_mapping.py::test_the_refund_path_refuses_an_unmapped_payment",
        "tests/integration/test_provider_mapping.py::test_a_mapping_in_the_other_environment_is_not_reported_as_missing",
        "tests/integration/test_provider_mapping.py::test_two_payments_cannot_claim_one_provider_object",
    ],
    "06 prompt injection": [
        "scenario:SEC-01", "scenario:TOOL-04",
        "tests/security/test_security.py::test_injection_in_customer_notes_does_not_cause_refund",
        "tests/security/test_security.py::test_untrusted_text_appears_only_inside_its_delimiters",
    ],
    "07 malformed provider response": [
        "scenario:UNK-06",
        "tests/unit/test_razorpay_contract.py::test_malformed_success_body_is_ambiguous_never_success",
        "tests/unit/test_razorpay_contract.py::test_read_5xx_raises_provider_error_not_httpx",
    ],
    "08 duplicate webhook": [
        "scenario:WHK-02",
        "tests/integration/test_webhooks.py::test_redelivery_is_recorded_once",
        "tests/integration/test_webhooks.py::test_the_same_event_id_with_a_different_body_is_a_conflict",
    ],
    "09 out-of-order webhook": [
        "tests/integration/test_webhooks.py::test_the_order_deliveries_arrive_in_does_not_change_the_outcome",
        "tests/integration/test_webhooks.py::test_a_stale_failure_arriving_late_does_not_regress_a_settled_refund",
    ],
    "10 provider timeout": [
        "scenario:UNK-01", "scenario:UNK-04", "scenario:UNK-05",
        "tests/unit/test_razorpay_contract.py::test_timeout_before_submit_is_not_submitted",
        "tests/unit/test_razorpay_contract.py::test_timeout_after_submit_is_submitted",
        "tests/integration/test_remediation.py::test_a_timeout_before_submit_is_failed_and_says_timeout",
    ],
    "11 worker retry": [
        "tests/integration/test_async_tasks.py::test_a_task_whose_worker_died_is_failed_not_retried",
        "tests/integration/test_webhooks.py::test_a_claimed_delivery_is_processed_once",
        "tests/integration/test_webhooks.py::test_a_delivery_that_keeps_failing_is_dead_lettered",
    ],
    "12 stale action": [
        "tests/integration/test_durability.py::test_a_lost_request_leaves_a_claim_the_sweep_can_finish",
        "tests/integration/test_durability.py::test_a_request_lost_before_the_provider_answered_leaves_a_pending_claim",
        "tests/integration/test_remediation.py::test_a_submitted_action_nobody_verified_is_not_hidden",
    ],
    "13 budget exceeded": [
        "scenario:SEC-19",
        "tests/integration/test_flows.py::test_budget_terminates_runaway_loop",
        "tests/integration/test_recovery.py::test_budget_refuses_a_spend_over_the_maximum",
    ],
    "14 customer attempt limit": [
        "tests/integration/test_recovery.py::test_budget_refuses_a_customer_already_approached",
        "tests/integration/test_campaign.py::test_an_attempt_counts_against_the_budget_even_when_it_fails",
    ],
    "15 UNKNOWN verification": [
        "scenario:UNK-01", "scenario:UNK-02",
        "tests/integration/test_flows.py::test_lost_response_yields_unknown_then_resolves",
        "tests/integration/test_remediation.py::test_an_unknown_refund_is_never_retried_by_reconciliation",
        "tests/integration/test_remediation.py::test_a_409_in_flight_is_unknown_and_reconciliation_only_reads",
    ],
    "16 replay side-effect attempt": [
        "tests/integration/test_flows.py::test_re_reason_makes_no_financial_side_effect",
        "tests/integration/test_remediation.py::test_a_replays_approval_can_never_be_executed",
        "tests/integration/test_remediation.py::test_a_replay_never_calls_the_provider_for_a_read",
    ],
    "17 unauthorized action": [
        "scenario:SEC-02", "scenario:TOOL-05",
        "tests/security/test_security.py::test_unauthorized_user_cannot_refund",
    ],
    "18 prohibited model tool request": [
        "scenario:SEC-24",
        "tests/security/test_security.py::test_model_cannot_call_unregistered_tool",
        "tests/integration/test_runtime_gates.py::test_an_unregistered_tool_reaches_neither_policy_nor_the_provider",
    ],
    "19 malformed tool response": [
        "tests/security/test_adversarial_matrix.py::test_a_malformed_tool_result_fails_closed",
    ],
    "20 LLM unavailable": [
        "tests/integration/test_remediation.py::test_a_model_that_fails_mid_run_is_a_recorded_fallback",
        "tests/integration/test_remediation.py::test_a_model_that_cannot_be_reached_is_an_unavailable_fallback",
        "tests/integration/test_remediation.py::test_with_fallback_disabled_an_unavailable_model_fails_the_run",
    ],
}


def _scenario_ids() -> set[str]:
    data = yaml.safe_load((ROOT / "data/scenarios/scenarios.yaml").read_text())
    items = data if isinstance(data, list) else data.get("scenarios", [])
    return {s["id"] for s in items}


def _test_names(path: str) -> set[str]:
    tree = ast.parse((ROOT / path).read_text())
    return {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}


def test_there_are_exactly_twenty():
    assert len(MATRIX) == 20


@pytest.mark.parametrize("scenario", sorted(MATRIX))
def test_every_mandatory_scenario_names_evidence_that_exists(scenario):
    refs = MATRIX[scenario]
    assert refs, f"{scenario} names no evidence"
    scenarios = _scenario_ids()
    missing = []
    for ref in refs:
        if ref.startswith("scenario:"):
            if ref.split(":", 1)[1] not in scenarios:
                missing.append(ref)
        else:
            path, name = ref.split("::")
            if not (ROOT / path).exists() or name not in _test_names(path):
                missing.append(ref)
    assert missing == [], f"{scenario}: cited evidence does not exist: {missing}"


# -------------------------------------------------- 19: malformed tool result
def test_a_malformed_tool_result_fails_closed(db, owner, monkeypatch):
    """A tool that returns something that is not a ToolResult must not become
    evidence, a finding, an approval or an action. The run fails -- recorded,
    with its trace -- rather than reasoning on top of a result nobody can read."""
    from app.agent.runtime import AgentRuntime, AgentRuntimeError
    from app.models import AgentAction, AgentTask, Approval, TaskStatus
    from app.tools import registry

    real = dict(registry._READ_IMPL)
    for name in real:
        monkeypatch.setitem(registry._READ_IMPL, name,
                            lambda *a, **k: {"not": "a ToolResult"})

    with pytest.raises(AgentRuntimeError) as e:
        AgentRuntime(db, owner).run("Find the duplicate payment and refund it.")
    db.rollback()

    task = db.get(AgentTask, e.value.task_id)
    assert task is not None and task.status is TaskStatus.FAILED
    assert task.failure_code == "INTERNAL_ERROR"
    assert db.query(Approval).filter(Approval.task_id == task.id).count() == 0
    assert db.query(AgentAction).filter(AgentAction.task_id == task.id).count() == 0

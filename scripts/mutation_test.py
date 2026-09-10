"""Mutation test for the evaluation suite — does it actually catch regressions?

A suite that reports 100/100 proves nothing on its own; it may simply not be
asserting anything. This script deliberately breaks each core safety control,
re-runs the suite, and reports which scenarios caught the break.

A mutation that NO scenario catches is a hole in the suite, not a success.

Every mutation is applied to a copy and reverted in a `finally`, so a crash
cannot leave the working tree modified.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from app.integrity import HARNESS_ENV, MARKER

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def _interpreter() -> str:
    """Resolve the interpreter for child processes.

    Local runs use the repo venv; CI has python on PATH and no venv. Hardcoding
    `.venv/bin/python` silently breaks CI, so it is only a fallback.
    """
    override = os.environ.get("MERCHANTOPS_PYTHON")
    if override:
        return override
    venv = ROOT / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


PY = _interpreter()

# (label, file, find, replace)
MUTATIONS = [
    (
        "policy: stop checking permissions",
        "app/policy/engine.py",
        "    missing = [p for p in required if p not in ctx.permissions]",
        "    missing = []  # MUTANT",
    ),
    (
        # NOTE: this anchor moved once already. Tenant isolation rewrote the
        # ownership check from `owner != ctx.merchant_id` to a mappings row, and
        # the mutation silently became a SKIP — a control with no mutant, which
        # the harness reports as a survivor rather than passing quietly. Worth
        # remembering that the harness is subject to the same drift it exists to
        # detect: an anchor is a copy of code kept somewhere else.
        "policy: stop enforcing merchant isolation",
        "app/policy/engine.py",
        '        if owner["merchant_id"] != ctx.merchant_id:',
        "        if False:  # MUTANT",
    ),
    (
        "policy: auto-approve HIGH risk instead of requiring a human",
        "app/policy/engine.py",
        "    return PolicyResult(\n        Decision.REQUIRE_APPROVAL,",
        "    return PolicyResult(\n        Decision.ALLOW,  # MUTANT",
    ),
    (
        "policy: drop the refund amount limit",
        "app/policy/engine.py",
        "        if amount > limit:",
        "        if False:  # MUTANT",
    ),
    (
        "policy: drop the duplicate-action guard",
        "app/policy/engine.py",
        "        if existing:",
        "        if False:  # MUTANT",
    ),
    (
        "verification: report SUCCESS whenever state is unreadable",
        "app/verification/engine.py",
        "            VerificationState.UNKNOWN,\n"
        "            f\"Could not read resulting payment state: {e}.",
        "            VerificationState.SUCCESS,\n"
        "            f\"MUTANT: {e}.",
    ),
    (
        "verification: trust the API response instead of reading state back",
        "app/verification/engine.py",
        "    if delta >= expected_refund_minor and (refund is None or refund.status == \"processed\"):",
        "    if True:  # MUTANT",
    ),
    (
        "runtime: stop rejecting unregistered tools",
        "app/agent/runtime.py",
        "        if spec is None:",
        "        if False:  # MUTANT",
    ),
    (
        "runtime: skip argument validation",
        "app/agent/runtime.py",
        "        ok, arg_err = validate_arguments(spec, req.arguments)",
        "        ok, arg_err = True, None  # MUTANT",
    ),
    (
        "runtime: remove the execution budget",
        "app/agent/runtime.py",
        "            if seq >= s.max_tool_calls_per_task:",
        "            if False:  # MUTANT",
    ),
    (
        "approval: stop checking expiry",
        "app/policy/engine.py",
        "    if exp < now:",
        "    if False:  # MUTANT",
    ),
    (
        "actions: let the caller reuse a spent idempotency key",
        "app/tools/actions.py",
        '    raw = f"{merchant_id}|{external_payment_id}|{action_type}|{approval_id}"',
        '    import uuid as _u; raw = _u.uuid4().hex  # MUTANT',
    ),
    (
        "verification: ignore the payment read-back entirely",
        "app/verification/engine.py",
        "    delta = payment.amount_refunded_minor - refunded_before_minor",
        "    delta = expected_refund_minor  # MUTANT: pretend the payment moved",
    ),
    (
        "actions: roll back the whole transaction on a duplicate action",
        "app/tools/actions.py",
        "        sp.rollback()",
        "        session.rollback()  # MUTANT",
    ),
    (
        "governance: let a higher score buy a critical regression",
        "app/eval/governance.py",
        "        if self.critical_regressions:",
        "        if False:  # MUTANT",
    ),
    (
        "governance: treat no scenario as critical",
        "app/eval/governance.py",
        '                          or c[sid]["metrics"].get("critical")),',
        "                          and False),  # MUTANT",
    ),
    (
        "reconcile: verify every action type with the refund verifier",
        "app/tools/actions.py",
        "    reverifier = REVERIFIERS.get(action.action_type)",
        '    reverifier = REVERIFIERS["refund"]  # MUTANT',
    ),
    (
        "reconcile: sweep states that are already determinations",
        "app/failures.py",
        "    return code is not None and classify(code).retryability is Retryability.RECONCILE",
        "    return code is not None  # MUTANT",
    ),
    (
        "tools: retry a failure the taxonomy says never to retry",
        "app/tools/registry.py",
        "        if result.success or not may_retry(result.error_code):",
        "        if result.success:  # MUTANT",
    ),
    (
        "webhooks: stop listening for a paid payment link",
        "app/webhooks/razorpay.py",
        '    "payment_link.paid", "payment_link.expired", "payment_link.cancelled",',
        "    # MUTANT",
    ),
    (
        "detection: treat unverified provider events as evidence",
        "app/detection/rules.py",
        "          AND signature_valid = true",
        "          AND true  -- MUTANT",
    ),
    (
        "metrics: report an unmeasurable metric as a number anyway",
        "app/metrics.py",
        '        "root_cause_accuracy", None, "ratio", False,',
        '        "root_cause_accuracy", 0.94, "ratio", True,  # MUTANT',
    ),
    (
        "metrics: let an untimed objective read as satisfied",
        "app/metrics.py",
        "                         None if v is None else v < SLO_POLICY_DECISION_MS,",
        "                         True,  # MUTANT",
    ),
    (
        "metrics: stop counting actions with no approval behind them",
        "app/metrics.py",
        "        WHERE a.merchant_id = :m AND (ap.id IS NULL OR ap.decision <> 'APPROVED')\n"
        '    """), {"m": merchant_id}).scalar() or 0)\n'
        '    out.append(Objective("unauthorized_executions"',
        "        WHERE a.merchant_id = :m AND false  -- MUTANT\n"
        '    """), {"m": merchant_id}).scalar() or 0)\n'
        '    out.append(Objective("unauthorized_executions"',
    ),
    (
        "messages: leave the final answer out of the transcript",
        "app/agent/runtime.py",
        '                self._say(task, messages, turn_no + 1, "assistant", turn.text)\n'
        "                break",
        "                break  # MUTANT",
    ),
    (
        "messages: drop the untrusted marker from the transcript",
        "app/agent/runtime.py",
        '            contains_untrusted="<untrusted_merchant_data" in blob,',
        "            contains_untrusted=False,  # MUTANT",
    ),
    (
        "messages: skip redaction on the stored conversation",
        "app/agent/runtime.py",
        "            content=redact(stored),",
        "            content=stored,  # MUTANT",
    ),
    (
        "tenancy: stop enforcing the tenant boundary",
        "app/policy/engine.py",
        '        if owner["tenant_id"] != ctx.tenant_id:\n'
        "            return PolicyResult(\n                Decision.DENY,\n"
        '                f"Payment {target_payment} belongs to another tenant. Cross-tenant "',
        "        if False:  # MUTANT\n"
        "            return PolicyResult(\n                Decision.DENY,\n"
        '                f"Payment {target_payment} belongs to another tenant. Cross-tenant "',
    ),
    (
        "tenancy: let the tenant check stand in for the merchant check on orders",
        "app/policy/engine.py",
        '        if owner is not None and owner["merchant_id"] != ctx.merchant_id:',
        "        if False:  # MUTANT",
    ),
    (
        "tenancy: take the tenant from the request instead of the database",
        "app/api/security.py",
        # `row` became an `authz.PrincipalRow` dataclass in ADR-0047, so this
        # anchor moved from subscripting to attribute access. The preflight
        # caught the drift on the first run after the change, which is the
        # difference between a stale anchor and a control nobody is testing.
        '    return Principal(row.tenant_id, row.user_id, row.merchant_id,',
        '    return Principal("TEN_KETTLE", row.user_id, row.merchant_id,  # MUTANT',
    ),
    (
        "failures: let an unknown financial state be retried",
        "app/failures.py",
        '        "UNKNOWN_EXTERNAL_STATE", Retryability.RECONCILE, Subsystem.RECONCILIATION,',
        '        "UNKNOWN_EXTERNAL_STATE", Retryability.BOUNDED_BACKOFF, Subsystem.RECONCILIATION,  # MUTANT',
    ),
    (
        "failures: treat an unclassified failure as retryable",
        "app/failures.py",
        '    "INTERNAL_ERROR", Retryability.ESCALATE, Subsystem.PLATFORM,',
        '    "INTERNAL_ERROR", Retryability.BOUNDED_BACKOFF, Subsystem.PLATFORM,  # MUTANT',
    ),
    (
        "failures: let a policy denial be retried",
        "app/failures.py",
        '        "POLICY_DENIED", Retryability.NEVER, Subsystem.POLICY,',
        '        "POLICY_DENIED", Retryability.BOUNDED_BACKOFF, Subsystem.POLICY,  # MUTANT',
    ),
    (
        "observability: give every audit event its own correlation id",
        "app/audit/trace.py",
        "        correlation_id=_CURRENT_CORRELATION.get(),\n        payload=redact(payload or {}),",
        '        correlation_id=__import__("uuid").uuid4().hex,  # MUTANT\n'
        "        payload=redact(payload or {}),",
    ),
    # ---------------------------------------------------------- ADR-0029
    (
        "durability: flush the action claim instead of committing it",
        "app/tools/actions.py",
        "    checkpoint(session)\n\n    refunded_before",
        "    session.flush()  # MUTANT\n\n    refunded_before",
    ),
    (
        "durability: let an unhandled error take the trace down with it",
        "app/agent/runtime.py",
        "                raise self._crash(exc) from exc",
        "                raise  # MUTANT",
    ),
    (
        "budget: let the configured budget outrun the host's own timeout",
        "app/config.py",
        "        return max(5, min(self.max_wall_clock_seconds, limit - PLATFORM_MARGIN_SECONDS))",
        "        return self.max_wall_clock_seconds  # MUTANT",
    ),
    (
        # The anchor carries `abandoned_cutoff` because it has to. `escalate_exhausted`
        # was added with an identical two-line predicate, the anchor matched twice, and
        # the harness reported a broken anchor -- which is the harness being subject to
        # the same drift it exists to detect, exactly as the note above predicted. The
        # cutoff line appears only in `find_unsettled`, which is the branch this mutant
        # is about.
        "reconciliation: stop looking at claims nobody finished",
        "app/verification/reconciler.py",
        "                        and_(AgentAction.verification_state.is_(None),\n"
        "                             AgentAction.status.in_(ABANDONABLE),\n"
        "                             AgentAction.updated_at <= abandoned_cutoff),",
        "                        and_(False,  # MUTANT\n"
        "                             AgentAction.verification_state.is_(None),\n"
        "                             AgentAction.status.in_(ABANDONABLE),\n"
        "                             AgentAction.updated_at <= abandoned_cutoff),",
    ),
    # ---------------------------------------------------------- ADR-0031
    (
        "observability: label metrics with the raw path instead of the route",
        "app/observability/middleware.py",
        '    return path or "<unmatched>"',
        '    return request.url.path  # MUTANT',
    ),
    (
        "observability: stop redacting secrets on their way to stdout",
        "app/observability/logs.py",
        "            entry.update(redact(extras))",
        "            entry.update(extras)  # MUTANT",
    ),
    (
        "migrations: let the baseline downgrade drop every table",
        "alembic/versions/20260901_dfdcbe8c6ce5_baseline_schema.py",
        '    raise NotImplementedError(\n        "Refusing to drop every table',
        '    return  # MUTANT\n    raise NotImplementedError(\n        "Refusing to drop every table',
    ),
    (
        "versioning: hardcode the tool registry version",
        "app/tools/registry.py",
        '    return "tools-" + hashlib.sha256(material.encode()).hexdigest()[:12]',
        '    return "tools-v1"  # MUTANT',
    ),
    (
        "ledger: count a payment link as recovered the moment it is sent",
        "app/recovery/dispatch.py",
        '        if link is not None and link.status == "paid":',
        "        if True:  # MUTANT",
    ),
    (
        "ledger: fold the unknown bucket into recovered",
        "app/recovery/dispatch.py",
        "    if state is not VerificationState.SUCCESS:\n"
        "        return _FROM_VERIFICATION.get(state, CandidateStatus.UNKNOWN), 0",
        "    if state is not VerificationState.SUCCESS:\n"
        "        return CandidateStatus.RECOVERED, action.amount_minor  # MUTANT",
    ),
    (
        "ledger: report gross charges instead of attributed exposure",
        "app/recovery/ledger.py",
        '          COALESCE(SUM(attributed_amount_minor) FILTER (\n'
        "              WHERE status <> 'INELIGIBLE'), 0)                       AS recoverable,",
        '          COALESCE(SUM(amount_minor) FILTER (\n'
        "              WHERE status <> 'INELIGIBLE'), 0)                       AS recoverable,  -- MUTANT",
    ),
    (
        "ledger: keep resolved incidents in the at-risk figure",
        "app/recovery/ledger.py",
        "        WHERE merchant_id = :m AND status = ANY(:open)\n"
        '    """), {"m": merchant_id, "open": list(_OPEN)}).scalar() or 0)',
        "        WHERE merchant_id = :m\n"
        '    """), {"m": merchant_id}).scalar() or 0)  # MUTANT',
    ),
    (
        "recovery: dispatch every intervention as a refund",
        "app/recovery/dispatch.py",
        "    template = _REQUEST.get(candidate.intervention)",
        "    template = _REQUEST[Intervention.REFUND]  # MUTANT",
    ),
    (
        "output: accept a claim citing evidence that does not exist",
        "app/agent/output.py",
        "        if not any(e in known_evidence_ids for e in f.evidence_ids):",
        "        if False:  # MUTANT",
    ),
    (
        "output: show a malformed agent output instead of failing the task",
        "app/agent/runtime.py",
        "            task.status = TaskStatus.FAILED\n            task.failure_code = problem.code",
        "            pass  # MUTANT",
    ),
    (
        "output: let the model's requires_human=false clear the approval flag",
        "app/api/main.py",
        '        "requires_human": bool(approvals) or task.model_requires_human,',
        '        "requires_human": task.model_requires_human,  # MUTANT',
    ),
    (
        "output: join the machine block onto the human answer",
        "app/agent/runtime.py",
        "        prose, output, problem = self._structured_output(task, answer)\n"
        "        answer = prose\n        task.final_answer = prose",
        "        prose, output, problem = self._structured_output(task, answer)\n"
        "        task.final_answer = answer  # MUTANT",
    ),
    (
        "output: restart evidence numbering on every tool call",
        "app/agent/runtime.py",
        "        rendered, self._evidence_seq = _render_tool_result(\n"
        "            structured, list(structured.get(\"evidence\", [])), self._evidence_seq)",
        "        rendered, _ = _render_tool_result(\n"
        "            structured, list(structured.get(\"evidence\", [])), 0)  # MUTANT",
    ),
    (
        "tools: let a customer-contacting action run on the read path",
        "app/tools/registry.py",
        '    "reconcile_transaction": reconcile_transaction,\n}',
        '    "reconcile_transaction": reconcile_transaction,\n'
        '    "generate_payment_link": get_payment,  # MUTANT\n}',
    ),
    (
        "tools: drop the permission on customer-contacting actions",
        "app/tools/recovery_actions.py",
        '    required_permissions=["action:recover"],\n    risk_class=RiskClass.MEDIUM,\n'
        '    audit_required=True,\n    idempotent=True,\n    reversible=False,\n)\n\n'
        'SPEC_NOTIFICATION',
        '    required_permissions=[],  # MUTANT\n    risk_class=RiskClass.MEDIUM,\n'
        '    audit_required=True,\n    idempotent=True,\n    reversible=False,\n)\n\n'
        'SPEC_NOTIFICATION',
    ),
    (
        "tools: send a payment link for a payment that did not fail",
        "app/tools/recovery_actions.py",
        '    if row["status"] != "failed":',
        "    if False:  # MUTANT",
    ),
    (
        "tools: contact a customer who has opted out",
        "app/tools/recovery_actions.py",
        '    if row["contact_opted_out"]:',
        "    if False:  # MUTANT",
    ),
    (
        "tools: stop deduplicating customer contact",
        "app/tools/recovery_actions.py",
        '    raw = f"{merchant_id}|{target}|{action_type}|{approval_id}"',
        '    import uuid as _u; raw = _u.uuid4().hex  # MUTANT',
    ),
    (
        "tools: report an unreadable notification as sent",
        "app/tools/recovery_actions.py",
        "        return VerificationResult(\n            VerificationState.UNKNOWN,\n"
        '            "The provider returned no record for this notification, so whether "',
        "        return VerificationResult(\n            VerificationState.SUCCESS,  # MUTANT\n"
        '            "The provider returned no record for this notification, so whether "',
    ),
    (
        "tools: return merchant free text as trusted data",
        "app/tools/investigation.py",
        '        ev.append(Evidence(key="customer_notes", value=c["notes"],\n'
        '                           source="customers.notes", untrusted=True))',
        '        ev.append(Evidence(key="customer_notes", value=c["notes"],\n'
        '                           source="customers.notes", untrusted=False))  # MUTANT',
    ),
    (
        "recovery: drop the campaign spend bound",
        "app/recovery/stopping.py",
        "        if prospective > plan.max_recovery_minor:",
        "        if False:  # MUTANT",
    ),
    (
        "recovery: drop the campaign action-count bound",
        "app/recovery/stopping.py",
        "    if taken >= plan.max_actions:",
        "    if False:  # MUTANT",
    ),
    (
        "recovery: record the stop instead of acting on it",
        "app/recovery/dispatch.py",
        # The trailing context that used to disambiguate this anchor was
        # `\n\n    request = `, and v2 §20 inserted a POLICY_EVALUATING move
        # between the two — turning the mutant into a silent SKIP. The line is
        # unique in the file on its own, so the anchor no longer depends on
        # what happens to follow it.
        "        raise RecoveryStopped(decision)",
        "        pass  # MUTANT",
    ),
    (
        "recovery: claim the whole failed volume was at risk",
        "app/recovery/planner.py",
        "        attributable = min(1.0, incident.revenue_at_risk_minor / total_volume)",
        "        attributable = 1.0  # MUTANT",
    ),
    (
        "recovery: grade a bulk action as if it stood alone",
        "app/recovery/dispatch.py",
        "    bulk = len(executable_candidates(session, plan))",
        "    bulk = 1  # MUTANT",
    ),
    (
        "risk: let computed risk replace the declared floor instead of raising it",
        "app/policy/risk.py",
        "    final = risk_at_least(declared, computed)",
        "    final = computed  # MUTANT",
    ),
    (
        "risk: never raise above the declared class",
        "app/policy/risk.py",
        "    return RiskAssessment(level=final, declared=declared, computed=computed,",
        "    return RiskAssessment(level=declared, declared=declared, computed=computed,  # MUTANT",
    ),
    (
        "approval: execute before enough people have signed",
        "app/agent/approval.py",
        "    if len(signatures) < ap.required_signatures:",
        "    if False:  # MUTANT",
    ),
    (
        "approval: forget that policy demanded two signatures",
        "app/agent/runtime.py",
        "            required_signatures=pol.required_signatures,",
        "            required_signatures=1,  # MUTANT",
    ),
    (
        "webhooks: accept any signature",
        "app/webhooks/razorpay.py",
        "    return hmac.compare_digest(expected, signature)",
        "    return True  # MUTANT",
    ),
    (
        "webhooks: stop deduplicating deliveries",
        "app/webhooks/razorpay.py",
        '    event_id = event_id_header or f"sha256:{payload_hash}"',
        '    event_id = __import__("uuid").uuid4().hex  # MUTANT',
    ),
    (
        "webhooks: process a delivery that failed its signature",
        "app/webhooks/razorpay.py",
        "    if status is not WebhookStatus.RECEIVED:",
        "    if False:  # MUTANT",
    ),
    (
        "webhooks: stop treating a provider contradiction as a mismatch",
        "app/webhooks/processing.py",
        "CONTRADICTS_SUCCESS = frozenset({VerificationState.FAILED, VerificationState.PARTIAL})",
        "CONTRADICTS_SUCCESS = frozenset()  # MUTANT",
    ),
    (
        "detection: stop deduplicating incidents",
        "app/detection/rules.py",
        'detection_key=f"{merchant_id}|PAYMENT_DEGRADATION|{method}|{cut.isoformat()}",',
        'detection_key=__import__("uuid").uuid4().hex,  # MUTANT',
    ),
    (
        "detection: drop the degradation threshold",
        "app/detection/rules.py",
        "        if drop_pp < DEGRADATION_THRESHOLD_PP:",
        "        if False:  # MUTANT",
    ),
    (
        "detection: read ordinary variance as the degradation onset",
        "app/detection/rules.py",
        "        if total < MIN_BUCKET_VOLUME:",
        "        if False:  # MUTANT",
    ),
    (
        "lifecycle: allow any incident transition",
        "app/incidents/lifecycle.py",
        "    if not is_legal(frm, to):",
        "    if False:  # MUTANT",
    ),
    (
        "incidents: resolve regardless of what the task actually did",
        "app/incidents/manager.py",
        "    target = _OUTCOME.get(out.status, S.ESCALATED)",
        "    target = S.RESOLVED  # MUTANT",
    ),
    (
        "audit: stop redacting secrets",
        "app/audit/trace.py",
        "        return {k: (\"[REDACTED]\" if _SECRET_KEYS.search(str(k)) else redact(v))",
        "        return {k: redact(v)  # MUTANT",
    ),
    # ---------------------------------------------------------- ADR-0033/0034
    # MerchantOps v2 §18 and §33. Two controls the suite had no mutant for
    # until the scenarios in COR-* and CNF-* existed to catch one.
    (
        "confidence: let the model's own number set the band",
        "app/agent/confidence.py",
        "    ceiling = _model_ceiling(model_confidence)\n"
        "    if _rank(ceiling) < _rank(a.band):",
        "    ceiling = _model_ceiling(model_confidence)\n"
        "    a.band = ceiling  # MUTANT\n"
        "    if _rank(ceiling) < _rank(a.band):",
    ),
    (
        "confidence: let a confident model raise the band",
        "app/agent/confidence.py",
        "    if _rank(ceiling) < _rank(a.band):",
        "    if False:  # MUTANT",
    ),
    (
        "confidence: count evidence rows instead of independent sources",
        "app/agent/confidence.py",
        "    a.independent_sources = len({getattr(e, \"source\", None) for e in trusted\n"
        "                                 if getattr(e, \"source\", None)})",
        "    a.independent_sources = len(trusted)  # MUTANT",
    ),
    (
        "confidence: let untrusted evidence corroborate",
        "app/agent/confidence.py",
        "    trusted = [e for e in evidence if not getattr(e, \"untrusted\", False)]",
        "    trusted = list(evidence)  # MUTANT",
    ),
    (
        "correlation: call every anomaly corroborated",
        "app/detection/correlation.py",
        '                "multivariate": c.corroboration > 1,',
        '                "multivariate": True,  # MUTANT',
    ),
    (
        "correlation: let a rule corroborate itself",
        "app/detection/correlation.py",
        "            others = [r for r in c.independent_rules if r != a.detection_rule]",
        "            others = list(c.independent_rules)  # MUTANT",
    ),
    (
        "correlation: put every anomaly in the same episode",
        "app/detection/correlation.py",
        "        if a.started_at - previous.started_at <= CORRELATION_WINDOW:",
        "        if True:  # MUTANT",
    ),
    (
        "correlation: count anomalies instead of the rules that produced them",
        "app/detection/correlation.py",
        "        return len(self.independent_rules)",
        "        return len(self.anomalies)  # MUTANT",
    ),
    # MerchantOps v2 §30. A hypothesis engine whose hypotheses cannot be
    # contradicted is a list of guesses with ceremony, so every mutant here
    # breaks the engine's ability to say no.
    (
        "hypotheses: accept every candidate instead of testing it",
        "app/evidence/hypotheses.py",
        "        if h.contradiction_count:",
        "        if False:  # MUTANT",
    ),
    (
        "hypotheses: report an untestable hypothesis as rejected",
        "app/evidence/hypotheses.py",
        "def _no_probe(detail: str) -> Probe:\n    return Probe(False, False, detail, {})",
        "def _no_probe(detail: str) -> Probe:\n"
        "    return Probe(False, True, detail, {})  # MUTANT",
    ),
    (
        "hypotheses: promote a contended explanation to the sole survivor",
        "app/evidence/hypotheses.py",
        "            h.status = (HypothesisStatus.SUPPORTED if len(supported) == 1\n"
        "                        else HypothesisStatus.CONTENDING)",
        "            h.status = HypothesisStatus.SUPPORTED  # MUTANT",
    ),
    (
        "hypotheses: stop drawing the verdict into the evidence graph",
        "app/evidence/hypotheses.py",
        "        if result.supports or result.contradicts:",
        "        if False:  # MUTANT",
    ),
    (
        "hypotheses: let scattered error codes still mean one failing provider",
        "app/evidence/hypotheses.py",
        "    if share >= 0.8:",
        "    if True:  # MUTANT",
    ),
    (
        "hypotheses: call flat traffic a traffic anomaly",
        "app/evidence/hypotheses.py",
        "    if abs(change) >= 0.5:",
        "    if True:  # MUTANT",
    ),
    (
        "evidence graph: draw an ungrounded conclusion as a root cause",
        "app/evidence/graph.py",
        "        if not f.get(\"evidence_refs\"):",
        "        if False:  # MUTANT",
    ),
    # MerchantOps v2 §37, §38. The campaign IS the plan, so these break the two
    # things that were genuinely missing rather than a second entity.
    (
        "campaign: read the global risk ceiling instead of the campaign's own",
        "app/recovery/stopping.py",
        '    ceiling = getattr(plan, "max_risk_level", None) or MAX_UNATTENDED_RISK',
        "    ceiling = MAX_UNATTENDED_RISK  # MUTANT",
    ),
    (
        "campaign: count only successful attempts against the budget",
        "app/recovery/campaign.py",
        '    spent_minor = sum(by_status[s]["attributed"] for s in\n'
        '                      ("ATTEMPTED", "RECOVERED", "FAILED", "UNKNOWN")\n'
        "                      if s in by_status)",
        '    spent_minor = sum(by_status[s]["attributed"] for s in ("RECOVERED",)\n'
        "                      if s in by_status)  # MUTANT",
    ),
    (
        "campaign: report no bound as ever exhausted",
        "app/recovery/campaign.py",
        "    if actions_taken >= plan.max_actions:",
        "    if False:  # MUTANT",
    ),
    # MerchantOps v2 §17. The seasonal baseline SUPPRESSES incidents, so every
    # mutant here is a way for it to hide a real one.
    (
        "baselines: let a baseline with no history veto an incident",
        "app/detection/baselines.py",
        "    if not baseline.measured:\n        return False",
        "    if False:  # MUTANT\n        return False",
    ),
    (
        "baselines: veto on a single observation instead of a baseline",
        "app/detection/baselines.py",
        "MIN_SLOT_SAMPLES = 3",
        "MIN_SLOT_SAMPLES = 1  # MUTANT",
    ),
    (
        "baselines: extrapolate a seasonal opinion over gaps in the history",
        "app/detection/baselines.py",
        "        if coverage < MIN_COVERAGE:",
        "        if False:  # MUTANT",
    ),
    # WITHDRAWN: "bucket slots in the server's timezone rather than UTC".
    # The UTC cast is correct and stays, but the defect it prevents is a
    # cross-environment disagreement -- two deployments placing the same payment
    # in different hours. A uniform offset shifts the current window and the
    # history together, so within one run the self-join pairs the same traffic
    # and nothing observable changes. A mutant nothing CAN catch is not a gap in
    # the suite; it is a mutant that does not describe a behaviour. See ADR-0038
    # and `test_the_baseline_does_not_move_with_the_servers_timezone`.
    (
        "baselines: suppress any drop the seasonal baseline can partly explain",
        "app/detection/baselines.py",
        "    return (baseline.expected_rate - float(current_rate)) < threshold_pp",
        "    return True  # MUTANT",
    ),
    # MerchantOps v2 §14, ADR-0040. The twin is a view over figures other
    # modules own, so its failure modes are disagreeing with them and inventing
    # what it cannot measure.
    (
        "state: report an unmeasurable latency as a number anyway",
        "app/state.py",
        '        "latency": {\n            "measured": False,',
        '        "latency": {\n            "measured": True, "p50_ms": 240,  # MUTANT',
    ),
    (
        "state: report GMV as revenue",
        "app/state.py",
        '        "gmv_minor": gmv,',
        '        "gmv_minor": revenue,  # MUTANT',
    ),
    (
        "state: recompute revenue at risk instead of reading the ledger",
        "app/state.py",
        '        "revenue_at_risk_minor": ledger.at_risk_minor,',
        '        "revenue_at_risk_minor": gmv - revenue,  # MUTANT',
    ),
    (
        "state: hand the model the whole twin instead of a slice",
        "app/state.py",
        '        relevant = [m for m in methods if method and m["method"] == method]',
        "        relevant = list(methods)  # MUTANT",
    ),
    (
        "state: let one merchant's twin read another's customers",
        "app/state.py",
        "          (SELECT COUNT(*) FROM customers WHERE merchant_id = :m)      AS active,",
        "          (SELECT COUNT(*) FROM customers)      AS active,  -- MUTANT",
    ),
    # MerchantOps v2 §20, ADR-0039. Each of these makes a state silently stop
    # being entered -- the failure mode the whole ADR is about, since a state
    # nothing enters looks exactly like a state nothing needed.
    (
        "lifecycle: stop reporting the agent's phases",
        "app/agent/runtime.py",
        "        if self.on_phase is None:\n            return",
        "        if True:  # MUTANT\n            return",
    ),
    (
        "lifecycle: skip the evidence phase and jump to the conclusion",
        "app/agent/runtime.py",
        '                    self._phase("evidence_collecting")',
        "                    pass  # MUTANT",
    ),
    (
        "lifecycle: let approval go straight to executing",
        "app/agent/approval.py",
        '    move_incident(session, task, _S.APPROVED, reason="Approval granted.")',
        "    pass  # MUTANT",
    ),
    (
        "lifecycle: leave the incident behind when the provider is called",
        "app/agent/approval.py",
        '    move_incident(session, task, _S.EXECUTING, reason="Provider action started.")',
        "    pass  # MUTANT",
    ),
    (
        "lifecycle: resolve without measuring what was recovered",
        "app/agent/approval.py",
        '        move_incident(session, task, _S.MEASURING,\n'
        '                      reason="Action verified; measuring the outcome.")',
        "        pass  # MUTANT",
    ),
    (
        "lifecycle: let an unknown external state skip reconciliation",
        "app/agent/approval.py",
        '        move_incident(session, task, _S.RECONCILING,\n'
        '                      reason="External state undetermined; handed to reconciliation.")',
        "        pass  # MUTANT",
    ),
    (
        "lifecycle: plan a recovery without moving the incident",
        "app/recovery/planner.py",
        "    _advance(session, incident, _S.RECOVERY_PLANNED,",
        "    _noop = (lambda *a, **k: None)(session, incident, _S.RECOVERY_PLANNED,  # MUTANT",
    ),
    (
        # Third attempt at this one, and the first two are worth recording.
        #
        #   "let advance move an incident illegally" removed a pre-check that
        #   duplicated `transition`'s own -> unobservable. The duplication was
        #   the real finding and the pre-check is gone.
        #
        #   Narrowing `except IllegalTransition` to a class never raised ->
        #   also unobservable, because the broad `except Exception` below it
        #   caught what the narrowed clause missed.
        #
        # Re-raising from inside the clause is the version that bites: a
        # sibling `except` does not catch it, so tolerance genuinely goes away.
        # That tolerance is the actual design decision in `advance` -- a path
        # that has already contacted a provider must not be taken down by an
        # incident it could not move.
        "lifecycle: make advance raise like transition does",
        "app/incidents/lifecycle.py",
        "    except IllegalTransition:",
        "    except IllegalTransition:\n        raise  # MUTANT",
    ),

    # ----------------------------------------------------------------------
    # Phase 2 controls — ADR-0046 to ADR-0051.
    #
    # Everything above grades the original control plane. These six subsystems
    # arrived afterwards and had tests but no mutants, which means the suite
    # could have lost any of them and reported the same number. Each mutation
    # below turns off exactly one thing a security review was told this system
    # does.
    # ----------------------------------------------------------------------
    (
        # The whole second wall. Without FORCE, PostgreSQL exempts a table's
        # OWNER from its own policies -- and the application IS the owner. The
        # policies would still be listed in pg_policies and would filter
        # nothing, which is the failure mode ADR-0046 was written around.
        "tenancy: enable row-level security without forcing it on the owner",
        "app/tenancy.py",
        '        out.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")',
        '        pass  # MUTANT',
    ),
    (
        # The binding that makes the policies mean anything. With no scope
        # pushed onto the transaction, every policy sees an empty
        # `app.merchant_id` and passes -- the documented "unbound is
        # unrestricted" state, reached by accident on an authenticated request.
        "tenancy: stop binding the principal to the transaction",
        "app/tenancy.py",
        "    scope = current_scope() if scope is None else scope",
        "    scope = UNBOUND  # MUTANT",
    ),
    # NOT a mutant: "drop WITH CHECK so the boundary applies to reads only".
    # It survived a spot-check and the reason is that it is EQUIVALENT, not that
    # the suite misses it. PostgreSQL applies the USING expression to writes as
    # well when no WITH CHECK is given, so removing it changes nothing --
    # verified by building a probe table with a USING-only policy and watching
    # an INSERT into another owner get refused anyway. Left here as a note so
    # nobody adds it again and spends an afternoon on it.
    (
        # ADR-0047. Permissions come from the role; granting every permission to
        # everybody is the failure a permissions model exists to prevent, and it
        # would look like a working system until somebody audited it.
        "authz: grant every principal every permission",
        "app/authz.py",
        '        email=row["email"], role=row["role"], permissions=list(row["permissions"]),',
        '        email=row["email"], role=row["role"],'
        ' permissions=["read:metrics", "read:orders", "action:refund",'
        ' "action:recover"],  # MUTANT',
    ),
    (
        # ADR-0048's offboarding, and ADR-0049 depends on it. Without the status
        # filter a DISABLED user resolves normally and their token keeps
        # working -- deprovisioning that changes a column and nothing else.
        "authz: let a disabled account keep authenticating",
        "app/authz.py",
        '    predicate = "u.id = :u" + (" AND u.status = \'ACTIVE\'" if active_only else "")',
        '    predicate = "u.id = :u"  # MUTANT',
    ),
    (
        # ADR-0049. An expired token that still works is a token with no expiry,
        # which is the limitation the whole ADR was written to close.
        "tokens: honour an expired token",
        "app/auth.py",
        '    if float(payload["exp"]) <= moment.timestamp():',
        "    if False:  # MUTANT",
    ),
    (
        # The individual revocation. "Sign out this session" becomes a database
        # write with no effect.
        "tokens: ignore the revocation list",
        "app/auth.py",
        '    revoked = session.execute(text(\n'
        '        "SELECT reason FROM revoked_tokens WHERE jti = :j"), {"j": claims.jti}).scalar()',
        '    revoked = None  # MUTANT',
    ),
    (
        # The wholesale revocation. Sign-out-everywhere, offboarding and the
        # response to a replayed refresh token all stop working together.
        "tokens: ignore the credentials-valid-from reset",
        "app/auth.py",
        "        if claims.iat < valid_from.timestamp():",
        "        if False:  # MUTANT",
    ),
    (
        # `typ` is what keeps a long-lived refresh token from being used as a
        # session credential. Same bytes to a signature check; different
        # meanings.
        "tokens: accept a refresh token wherever an access token is expected",
        "app/auth.py",
        '    if payload["typ"] != expect:',
        "    if False:  # MUTANT",
    ),
    (
        # ADR-0050. The nonce binds the ID token to THIS sign-in; without it a
        # token obtained in one attempt can be replayed into another.
        "sso: accept an ID token that answers a different sign-in",
        "app/sso.py",
        '    if claims.get("nonce") != flow["nonce"]:',
        "    if False:  # MUTANT",
    ),
    (
        # Without the audience check, a token minted for any other client of the
        # same provider is accepted here.
        "sso: accept an ID token issued for another client",
        "app/sso.py",
        '    if provider["client_id"] not in audiences:',
        "    if False:  # MUTANT",
    ),
    (
        # The replay guard on the callback. A captured redirect can be used
        # again.
        "sso: let a completed sign-in be replayed",
        "app/sso.py",
        '    if flow["consumed_at"] is not None:',
        "    if False:  # MUTANT",
    ),
    (
        # An unverified address is one somebody claimed. Matching an account on
        # it lets anyone who can add an address at the customer's IdP sign in as
        # somebody else.
        "sso: trust an unverified email address",
        "app/sso.py",
        '    if claims.get("email_verified") is False:',
        "    if False:  # MUTANT",
    ),
    (
        # ADR-0051. Deprovisioning that leaves live sessions is deprovisioning
        # in name only -- the exact gap SCIM was added to close.
        "scim: deactivate without revoking the person's tokens",
        "app/scim.py",
        "    auth.revoke_all_for(session, row[\"id\"])",
        "    pass  # MUTANT",
    ),

    # --- retention (§38) ---------------------------------------------------
    (
        # The compliance record, swept away by housekeeping. `audit_logs` is
        # append-only by trigger precisely so this cannot happen by accident;
        # a retention policy that learns to delete from it is the accident.
        "retention: let housekeeping prune the audit log",
        "app/retention.py",
        "        if policy.days is None:\n            continue",
        "        if False:  # MUTANT\n            continue",
    ),
    (
        # Age alone deletes events nobody ever delivered. A pending outbox row
        # is not stale, it is unfinished.
        "retention: drop outbox rows nothing ever published",
        "app/retention.py",
        "        if policy.eligible:",
        "        if False:  # MUTANT",
    ),

    # --- personal data at rest (ADR-0052) ----------------------------------
    (
        # The whole change, undone. Every value would be written in plaintext
        # while the column, the map and the ADR all still say "encrypted" --
        # which is worse than never encrypting, because a reviewer sees the
        # claim and stops asking.
        "privacy: write personal data in plaintext",
        "app/crypto.py",
        "    if is_encrypted(value):",
        "    if True:  # MUTANT",
    ),
    (
        # The blind index stops matching what the lookups it replaces matched.
        # `lower(email) = :e` was case-insensitive, so the symptom is not an
        # error: it is a second account for somebody who already had one.
        "privacy: stop normalising the blind index",
        "app/crypto.py",
        '    return hmac.new(key, f"{table}.{column}:{value.strip().lower()}".encode(),',
        '    return hmac.new(key, f"{table}.{column}:{value}".encode(),  # MUTANT',
    ),
    (
        # A deployment encrypting with the key published in this repository,
        # and nothing saying so.
        "privacy: let a deployment run on the development encryption key",
        "app/crypto.py",
        '    if not DEV_KEY_IN_USE or os.environ.get("MERCHANTOPS_ALLOW_DEV_SECRET"):',
        "    if True:  # MUTANT",
    ),

    # --- the mapping layer (MerchantOps §6, plan P0-02) --------------------
    (
        # The direction §6 is actually about. With ownership unchecked, one
        # merchant's synthetic id resolves against another merchant's payment,
        # which is a refund placed on somebody else's money.
        "mapping: stop checking who owns the payment being resolved",
        "app/integrations/mapping.py",
        "    if owner != merchant_id:",
        "    if False:  # MUTANT",
    ),
    (
        # A mapping is only meaningful within one provider universe. Ignoring
        # the environment makes a Test Mode id resolve for a live process and
        # the reverse -- the failure this table was introduced to make
        # impossible.
        "mapping: resolve without regard to the provider environment",
        "app/integrations/mapping.py",
        # `record_mapping` carries a byte-identical WHERE clause, so the anchor
        # reaches up to the SELECT list, which differs. Only `resolve` is under
        # test here -- a write that ignores the environment is refused by
        # `uq_mapping_payment_provider_env`, a read that ignores it is not
        # refused by anything.
        "        FROM provider_mappings\n"
        "        WHERE payment_id = :p AND provider = :prov AND environment = :env",
        "        FROM provider_mappings\n"
        "        WHERE payment_id = :p AND provider = :prov  -- MUTANT",
    ),
    (
        # A retired mapping must not execute. Treating it as live is how an id
        # somebody deliberately withdrew gets money moved against it.
        "mapping: execute against a retired mapping",
        "app/integrations/mapping.py",
        "    if row[\"status\"] != \"ACTIVE\":",
        "    if False:  # MUTANT",
    ),

    # --- reconciliation as durable work (plan P0-04, P0-15) ---------------
    (
        # Never escalating turns the stopping rule off: an action nothing can
        # settle is re-read forever and never reaches a human, which is the
        # failure UNKNOWN exists to prevent restated one level up.
        "reconciliation: never give up, so nothing ever reaches a human",
        "app/verification/schedule.py",
        "    return action.verify_attempts >= MAX_ATTEMPTS",
        "    return False  # MUTANT",
    ),
    (
        # The repair pass is what guarantees an action that reached the limit
        # by some route other than the sweep's own last pass is still handed
        # over. Without it such an action appears in neither queue.
        "reconciliation: stop repairing actions that ran out of attempts elsewhere",
        "app/verification/reconciler.py",
        "    for action in stuck:",
        "    for action in []:  # MUTANT",
    ),
    (
        # Escalating a settled action hands a human a finished job, and does it
        # for every action that took five attempts to succeed.
        "reconciliation: escalate actions that already settled",
        "app/verification/schedule.py",
        "    if is_settled(action.verification_state):\n        return False",
        "    if False:  # MUTANT\n        return False",
    ),

    # --- detection §12 ------------------------------------------------------
    (
        # The load-bearing decision in the failure-spike rule. The lost revenue
        # is already on the degradation incident; claiming it again roughly
        # doubles the at-risk figure on the Command Center.
        "detection: let a diagnostic rule claim revenue that is already counted",
        "app/detection/rules.py",
        "            revenue_at_risk_minor=0,\n            signals={\n"
        "                **_canonical(baseline=prev, observed=cur,",
        "            revenue_at_risk_minor=cur * 100000,  # MUTANT\n            signals={\n"
        "                **_canonical(baseline=prev, observed=cur,",
    ),
    (
        # Three failures becoming six is a doubling and is noise. Without the
        # floor the rule raises an incident for every rare error code.
        "detection: drop the volume floor under the failure-spike rule",
        "app/detection/rules.py",
        "        if cur < MIN_FAILURE_VOLUME:",
        "        if False:  # MUTANT",
    ),
    (
        # Reporting the whole refunded total describes ordinary business as an
        # incident: a merchant who always refunds £10k would be told £25k is at
        # risk the week they refund £25k.
        "detection: report total refunds rather than the excess over baseline",
        "app/detection/rules.py",
        "    excess_value = max(0, cur_v - prev_v)",
        "    excess_value = cur_v  # MUTANT",
    ),
    (
        # A merchant's first week of refunds is not an anomaly, and calling it
        # one greets every new account with an incident.
        "detection: treat a first week of refunds as unusual",
        "app/detection/rules.py",
        "    if prev_n == 0 and prev_v == 0:\n        # No baseline",
        "    if False:  # MUTANT\n        # No baseline",
    ),
    # --- ADR-0053: the architecture remediation -------------------------------
    # Each control the remediation added gets a mutant, for the reason the
    # rest of this list exists: a control nothing would notice losing is a
    # control in name only.
    (
        # The header Razorpay's refund API reads. Sending the payment one means
        # no provider-side idempotency on a refund at all.
        "provider: send refunds with the payment idempotency header",
        "app/integrations/razorpay/adapter.py",
        'REFUND_IDEMPOTENCY_HEADER = "X-Refund-Idempotency"',
        'REFUND_IDEMPOTENCY_HEADER = "X-Payment-Idempotency"  # MUTANT',
    ),
    (
        # A 5xx after the request reached Razorpay says nothing about whether it
        # was applied. Reading it as a definite failure invites a retry.
        "provider: read a 5xx as a definite failure",
        "app/integrations/razorpay/adapter.py",
        "        if resp.status_code >= 500 or resp.status_code == 409:",
        "        if resp.status_code == 409:  # MUTANT",
    ),
    (
        "boundaries: let a provider call run with writes held open",
        "app/boundaries.py",
        '    if mode == "off" or not open_write(session):',
        "    if True:  # MUTANT",
    ),
    (
        # The scope is SET LOCAL. Without the begin hook, a mid-request commit
        # sheds it and the rest of the request runs outside row-level security.
        "tenancy: stop re-applying the scope on each transaction",
        "app/db.py",
        "    apply_scope(connection)\n",
        "    pass  # MUTANT\n",
    ),
    (
        "approval: execute a replay's approval",
        "app/agent/approval.py",
        "    if task.is_replay:",
        "    if False:  # MUTANT",
    ),
    (
        "approval: execute a payload changed after approval",
        "app/agent/approval.py",
        "    if ap.policy_input_hash is not None and ap.policy_input_hash != policy_input_hash(",
        "    if False and ap.policy_input_hash != policy_input_hash(  # MUTANT",
    ),
    (
        "idempotency: accept a changed request under a reused key",
        "app/idempotency.py",
        "    if existing.request_hash != digest:",
        "    if False:  # MUTANT",
    ),
    (
        "replay: let a replay read the provider live",
        "app/agent/runtime.py",
        "            if self.frozen_tools is not None:",
        "            if False:  # MUTANT",
    ),
    (
        # A run the planner finished, recorded as the model's.
        "runtime: record a fallback run as a model result",
        "app/agent/runtime.py",
        "        self.ai_mode = (AIMode.AI_FAILED_FALLBACK if self._model_turns",
        "        self.ai_mode = (AIMode.AI_SUCCESS if self._model_turns  # MUTANT",
    ),
]


def run_suite() -> tuple[int, int, list[str]]:
    """A crash is NOT a pass. run_scenarios.py exits 0 only when every scenario
    passed and 1 when some failed; any other code means it died, in which case
    the report on disk is stale and must not be read as a result."""
    import json
    report = ROOT / "data" / "evaluation_report.json"
    # Delete the report first. run_scenarios.py writes it only after every
    # scenario has run, so a missing file unambiguously means the suite died
    # mid-run — and a stale file can never be misread as this run's result.
    report.unlink(missing_ok=True)
    subprocess.run([PY, "scripts/run_scenarios.py"], cwd=ROOT,
                   capture_output=True, text=True,
                   env={**os.environ, "PYTHONPATH": str(ROOT),
                        HARNESS_ENV: "1"})
    if not report.exists():
        return 0, 0, ["<suite crashed mid-run>"]
    rep = json.loads(report.read_text())
    failed = [x["scenario_id"] for x in rep["results"] if not x["passed"]]
    return rep["passed"], rep["total"], failed


def run_tests() -> tuple[bool, str]:
    # Inherit the environment. Replacing it wholesale drops DATABASE_URL and a
    # PATH the interpreter may need, which fails anywhere but a local dev box.
    r = subprocess.run([PY, "-m", "pytest", "tests", "-q", "--no-header", "-x"],
                       cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT),
                            HARNESS_ENV: "1"})
    line = [l for l in r.stdout.splitlines() if "passed" in l or "failed" in l]
    return r.returncode == 0, (line[-1] if line else "no output")


# Written at the end of every run, complete or filtered.
# `data/evaluation_report.json` is the model for this: the number a
# README publishes should come out of a file something produced, not out
# of somebody's memory of a terminal that has since scrolled away. It is
# git-ignored for the same reason its sibling is -- it is a measurement of
# a tree, not a property of one, and committing it would put a stale
# number under version control and start a merge conflict per run.
REPORT = ROOT / "data" / "mutation_report.json"

# Every file this run has rewritten. The end-of-run check compares against THIS
# rather than against whole directories -- see `_verify_tree_restored`.
TOUCHED: set[str] = set()
# The commit the run MEASURES, captured before the first mutation rather
# than when the report is written. The report used to record HEAD at the
# end, which on a run lasting two and a half hours is whatever somebody
# committed while it worked -- so it claimed to have measured a tree it
# had never seen. `check_counts.py` decides whether the score still holds
# by asking what changed in `app/` since this commit, and that question is
# meaningless against the wrong one.
MEASURED_TREE: str | None = None
MEASURED_CLEAN: bool | None = None


def _hms(seconds: float) -> str:
    """`1h04m`, `52m`, `40s`. Two units at most: a duration printed to the
    second is read as a measurement, and this is an estimate."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    if s >= 60:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s}s"

# The same path `app.integrity` refuses to start on, and the flag that exempts
# this process from it. Imported rather than repeated: a guard naming a
# different file from the one written here would be a guard that never fires.
LOCK = MARKER


def _hold(lock: Path, relpath: str | None, original: str | None) -> None:
    """Record which file is currently mutated, and what it said before.

    The `finally` that reverts a mutation only runs if the process gets to run
    it. SIGKILL, a killed container, a terminal closed on a foreground run --
    none of those do, and each leaves a rewritten safety control on disk that
    looks exactly like source someone wrote.

    That is not hypothetical. It has happened twice: once leaving
    `Decision.REQUIRE_APPROVAL` rewritten to `Decision.ALLOW` (every HIGH-risk
    action auto-approving) and once leaving a re-verifier pinned to refunds.
    Both were found by grep rather than by anything that would have stopped a
    commit.

    So the original is written to the lock BEFORE the mutation is applied. The
    next run restores from it instead of asking a human to notice.
    """
    import json

    lock.write_text(json.dumps({
        "note": "Mutation test in progress. Source files are being rewritten.",
        "file": relpath,
        "original": original,
        # Progress, for the human reading `make mutants-status` rather than for
        # the recovery path. A run of this length gets asked "how far in is it?"
        # and the answer used to be unavailable without reading the log.
        "rewritten_so_far": sorted(TOUCHED),
    }, indent=2))


def _recover(lock: Path) -> bool:
    """Restore a file a killed run left mutated. False if the tree is unsafe.

    A lock with no payload is either a run in progress right now or one from
    before this recovery existed; neither can be restored from, so it still
    stops and asks. A lock WITH a payload is a killed run, and the file it names
    is put back.
    """
    import json

    if not lock.exists():
        return True

    try:
        held = json.loads(lock.read_text())
    except (ValueError, OSError):
        held = {}

    relpath, original = held.get("file"), held.get("original")
    if relpath and original is not None:
        path = ROOT / relpath
        if path.read_text() != original:
            path.write_text(original)
            print(f"!! A previous run was killed with {relpath} mutated.")
            print("!! It has been restored from the lock file.")
        lock.unlink(missing_ok=True)
        return True

    print("A mutation run is already in progress, or a previous one was killed "
          "before it could record what it was rewriting.")
    print(f"Verify the tree ('git status', and grep for '# MUTANT' under app/) "
          f"and remove {lock.name} before retrying.")
    return False


def main() -> int:
    # Optional substring filters. A full run is 20 mutants x (scenario suite +
    # test suite) and takes well over half an hour, which is too slow to sit in
    # the middle of a change. `mutation_test.py detection lifecycle` runs only
    # the mutants whose label matches. CI still runs all of them.
    # The shard is parsed first so its VALUE can be excluded from the
    # selectors. `--shard 1/4` puts a bare `1/4` in argv, which does not start
    # with `-` and was therefore read as a label to match -- every shard then
    # selected nothing and exited "no mutants", which is a filtered run
    # reporting as a configuration error rather than grading anything.
    shard = _shard_arg()
    selectors = [a for a in _selector_args() if not a.startswith("-")]
    mutations = [m for m in MUTATIONS
                 if not selectors or any(s.lower() in m[0].lower() for s in selectors)]

    # `--shard N/M` splits the corpus across parallel jobs. The complete run is
    # 3h40m against a 360-minute ceiling on a hosted runner, which is twenty
    # minutes of headroom measured on a faster machine than CI has -- so this
    # exists before the corpus grows past it rather than after.
    #
    # ROUND-ROBIN, not contiguous blocks. Mutants for one file sit together in
    # `MUTATIONS` and cost roughly the same to grade, so contiguous shards would
    # hand one job every slow mutant in a file and another every cheap one. Every
    # Mth entry mixes them.
    if shard:
        index, total = shard
        mutations = shard_of(mutations, index, total)
        if not mutations:
            print(f"Shard {index}/{total} has no mutants. Use fewer shards.")
            return 1
    if not mutations:
        print(f"No mutation label matches {selectors}.")
        return 1

    print("=" * 78)
    print("Mutation test — breaking each control to prove the suite catches it")
    print("=" * 78)
    print()
    if selectors:
        print(f"!! FILTERED RUN: {len(mutations)}/{len(MUTATIONS)} mutants "
              f"matching {selectors}.")
        print("!! A filtered run is not a substitute for the full one.")
        print()
    print("!! Source files under app/ are REWRITTEN while this runs.")
    print("!! Do not commit, branch, or stash until it finishes.")
    print(f"!! Lock file: {LOCK.name}")
    print()

    if not _recover(LOCK):
        return 1
    _hold(LOCK, None, None)

    global MEASURED_TREE, MEASURED_CLEAN
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          cwd=ROOT, capture_output=True, text=True)
    MEASURED_TREE = head.stdout.strip() or None
    # And whether the code about to be measured IS that commit. Captured here,
    # before the first mutation makes the tree dirty by design.
    #
    # A hash alone says which commit was checked out, not what was in the
    # files: a score taken with uncommitted edits present is a score for a tree
    # nobody else has, and recording only the hash makes it indistinguishable
    # from one taken on the commit itself. That is ADR-0035's second failure --
    # an artifact with no conditions attached -- in the artifact that ADR was
    # written to produce.
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "app", "alembic"],
                           cwd=ROOT, capture_output=True, text=True)
    MEASURED_CLEAN = dirty.returncode == 0 and not dirty.stdout.strip()

    # Preflight. An anchor is a copy of code kept somewhere else, so it drifts
    # when the code moves — and a drifted anchor is reported as a SKIP that
    # counts as a survivor, fifty minutes into a run. Checking first turns that
    # into a second.
    stale = [(label, relpath) for label, relpath, find, _ in mutations
             if find not in (ROOT / relpath).read_text()]
    if stale:
        print("ANCHORS NO LONGER MATCH THE SOURCE — the code moved under them:")
        for label, relpath in stale:
            print(f"  {label}\n    in {relpath}")
        print("\nFix the anchors before running. A mutation that cannot be applied "
              "is a control with no test, not a control that passed.")
        LOCK.unlink(missing_ok=True)
        return 1

    baseline_pass, baseline_total, baseline_failed = run_suite()
    print(f"\nbaseline: {baseline_pass}/{baseline_total} scenarios pass", flush=True)
    if baseline_failed:
        print(f"  baseline is not clean ({baseline_failed}); aborting.")
        return 1

    survivors = []
    invalid = []
    rows = []

    # Progress, one line per mutant, as it happens.
    #
    # This used to accumulate every row in memory and print the table at the
    # end. A full run is 88 mutants and takes over two and a half hours, so
    # for that whole time the only output was the banner: no way to tell
    # mutant 3 from mutant 80, whether a suite had wedged, or whether anything
    # was being caught. `flush=True` because stdout redirected to a file is
    # block-buffered, which is how a run gets started and then shows one line
    # for two hours -- the caller should not have to know to pass `-u`.
    started = time.monotonic()

    def progress(n: int, label: str, status: str, detail: str) -> None:
        done = time.monotonic() - started
        # Remaining time from the average so far, which is what a reader
        # actually wants from a bar. Only after two, because one sample of a
        # 90-second step extrapolated over 88 of them is a guess presented as
        # an estimate.
        eta = f"  eta {_hms((done / n) * (len(mutations) - n))}" if n >= 2 else ""
        print(f"[{n:>2}/{len(mutations)}] {label:<52} {status:<9} {detail}"
              f"  ({_hms(done)}{eta})", flush=True)

    for n, (label, relpath, find, replace) in enumerate(mutations, 1):
        path = ROOT / relpath
        original = path.read_text()
        if find not in original:
            rows.append((label, "SKIP", "anchor not found", ""))
            survivors.append(label)
            progress(n, label, "SKIP", "anchor not found")
            continue
        try:
            TOUCHED.add(relpath)
            # Recorded before the write, so a kill between the two lines still
            # leaves the lock naming a file that is not yet mutated -- which
            # restores to a no-op rather than to the wrong content.
            _hold(LOCK, relpath, original)
            # setdefault: the FIRST reading is the one to compare against, so
            # two mutants in one file do not let the second record a mutated
            # state as this run's baseline.
            _ORIGINALS.setdefault(relpath, original)
            path.write_text(original.replace(find, replace, 1))
            passed, total, failed = run_suite()
            crashed = failed == ["<suite crashed mid-run>"]
            caught = 0 if crashed else (total - passed)
            tests_ok, test_line = run_tests()

            if crashed and tests_ok:
                # The scenario suite fell over and the unit tests saw nothing.
                # NOTHING was established, so this is a non-result rather than
                # a catch. Scoring a crash as `caught = 1` is the single
                # direction this harness must not be generous in: it read that
                # way until a run where two mutants crashed because the
                # scenario suite was being EDITED underneath it, and both
                # reported CAUGHT.
                #
                # The `and tests_ok` is a correction to that correction. The
                # two suites are INDEPENDENT signals, and a crash in one does
                # not invalidate a verdict from the other -- discarding a real
                # unit-test catch because the scenario runner died is the same
                # error in the opposite direction. `advance raise like
                # transition` was reported ungraded while pytest was reporting
                # "38 passed, 1 error" about it.
                invalid.append(label)
                rows.append((label, "NO RESULT",
                             "the suite crashed and no test caught it", ""))
            elif caught == 0 and tests_ok:
                survivors.append(label)
                rows.append((label, "SURVIVED", "no scenario or test caught it", ""))
                progress(n, label, "SURVIVED", "no scenario or test caught it")
            else:
                # Reached when scenarios caught it, or when the scenario suite
                # crashed but the unit tests still returned a verdict.
                detail = ("suite crashed" if crashed else f"{caught} scenario(s)")
                if not tests_ok:
                    detail += " + unit tests"
                rows.append((label, "CAUGHT", detail,
                             ", ".join(failed[:4]) + ("…" if len(failed) > 4 else "")))
                progress(n, label, "CAUGHT", detail)
        finally:
            path.write_text(original)
            _hold(LOCK, None, None)

    print()
    print(f"{'mutation':<52} {'result':<10} {'caught by'}")
    print("-" * 78)
    for label, status, detail, who in rows:
        print(f"{label:<52} {status:<10} {detail}")
        if who:
            print(f"{'':<52} {'':<10} {who}")

    LOCK.unlink(missing_ok=True)

    print()
    caught_n = sum(1 for r in rows if r[1] == "CAUGHT")
    _write_report(rows, caught_n, mutations, shard)

    # The evaluation report on disk now describes the LAST MUTANT's run -- a
    # deliberately broken tree. `run_suite` already deletes it before each
    # mutant so a stale file can never be misread as that mutant's result; the
    # same argument applies at the end, and did not used to. Left behind, it
    # makes `scripts/check_counts.py` report "scenarios-passed is published as
    # 167, measured 166" with nothing on screen explaining that the 166 came
    # from code somebody deliberately broke two hours ago.
    stale = ROOT / "data" / "evaluation_report.json"
    if stale.exists():
        stale.unlink()
        print(f"removed {stale.relative_to(ROOT)} -- it described a mutant, "
              f"not this tree. Re-run `make eval` for a real one.")
    graded = len(mutations) - len(invalid)
    print(f"RESULT: {caught_n}/{graded} mutations caught"
          + (f" ({len(invalid)} not graded)" if invalid else ""))

    if invalid:
        # Reported separately and loudly. A mutant whose run crashed has no
        # result, and rolling it into either column would turn "we do not know"
        # into a verdict -- the same mistake UNKNOWN, INSUFFICIENT and UNTESTED
        # exist to prevent elsewhere in this codebase.
        print("\nNOT GRADED — the suite crashed; re-run these:")
        for label in invalid:
            print(f"  - {label}")
    if survivors:
        print("\nSURVIVING MUTATIONS — these are gaps in the suite:")
        for label in survivors:
            print(f"  - {label}")
    if survivors or invalid:
        return 1
    print("Every injected defect was detected.")
    return 0




def _selector_args() -> list[str]:
    """argv without the shard flag and, when written apart, its value."""
    out, skip = [], False
    for arg in sys.argv[1:]:
        if skip:
            skip = False
            continue
        if arg == "--shard":
            skip = True          # the value follows as its own argument
            continue
        if arg.startswith("--shard="):
            continue
        out.append(arg)
    return out


def shard_of(mutations: list, index: int, total: int) -> list:
    """The Nth of M shards of the corpus.

    Its own function so the tests can exercise the real selection rather than a
    reimplementation of it -- a partition test that recomputes the formula it is
    checking passes whatever the harness does.
    """
    return [m for i, m in enumerate(mutations) if i % total == index - 1]


def _shard_arg() -> tuple[int, int] | None:
    """`--shard N/M`, validated. Returns None when the whole corpus is wanted."""
    for arg in sys.argv[1:]:
        if not arg.startswith("--shard"):
            continue
        raw = arg.split("=", 1)[1] if "=" in arg else sys.argv[sys.argv.index(arg) + 1]
        try:
            index, total = (int(x) for x in raw.split("/"))
        except (ValueError, IndexError):
            raise SystemExit(f"--shard wants N/M, got {raw!r}") from None
        if not 1 <= index <= total:
            raise SystemExit(f"--shard {raw}: N must be between 1 and M")
        return index, total
    return None


def _write_report(rows, caught_n: int, mutations, shard=None) -> None:
    """Record the run so a published number can be checked against it.

    `complete` is the field that matters. A filtered run measures a subset and
    its ratio is not the project's mutation score; recording WHICH kind of run
    this was is what stops a `scripts/mutation_test.py webhooks` result being
    read later as though it covered everything.

    `tree` is the commit the run measured. A report is a measurement of one
    tree, and a reader comparing it to a different tree should be able to see
    that rather than infer it.
    """
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps({
        "generated_at": datetime.now(UTC).isoformat(),
        "tree": MEASURED_TREE,
        "tree_clean": MEASURED_CLEAN,
        # A shard is never complete on its own. `scripts/merge_mutation_reports.py`
        # sets this true only when the shards together cover every mutant
        # exactly once, on one tree -- earned rather than asserted.
        "complete": shard is None and len(mutations) == len(MUTATIONS),
        "shard": None if shard is None else {"index": shard[0], "total": shard[1]},
        "labels": [m[0] for m in mutations],
        "defined": len(MUTATIONS),
        "run": len(mutations),
        "caught": caught_n,
        "survived": [label for label, status, _, _ in rows if status == "SURVIVED"],
        "mutants": [
            {"label": label, "status": status, "caught_by": detail,
             # The scenarios that graded it red, which is what separates a
             # mutant a scenario catches from one only a unit test does -- the
             # distinction every honest reading of the score depends on.
             "scenarios": [x for x in who.replace("…", "").split(", ") if x]}
            for label, status, detail, who in rows
        ],
    }, indent=2) + "\n")
    print(f"wrote {REPORT.relative_to(ROOT)}")



# What each mutated file said before this run touched it, recorded as the run
# goes. `_verify_tree_restored` compares against this rather than against HEAD.
_ORIGINALS: dict[str, str] = {}


def _verify_tree_restored() -> None:
    """Every mutation is reverted in a `finally`, but a process killed between
    the write and the revert leaves a mutant on disk. Say so loudly rather than
    leaving a broken working tree looking clean.

    Compared against **what this run found**, not against HEAD.

    It used to run `git diff --name-only -- app scripts alembic` and call every
    dirty file a mutation artifact, with `git checkout --` as the advice. On a
    clean tree that is right. On the ordinary tree of someone in the middle of a
    change — the only tree anyone runs this from — it names their uncommitted
    work and tells them to discard it. A safety check whose remedy destroys the
    work it was protecting is worse than no check.

    Comparing against the recorded originals cannot make that mistake: a file
    this run never mutated is never mentioned, however dirty it is.
    """
    stranded = [relpath for relpath, original in _ORIGINALS.items()
                if (ROOT / relpath).read_text() != original]
    if stranded:
        print("\n" + "!" * 78)
        print("MUTATION ARTIFACTS LEFT ON DISK — do not commit:")
        for f in stranded:
            print(f"  {f}")
        print("Restore with:  git checkout -- " + " ".join(stranded))
        print("!" * 78)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        LOCK.unlink(missing_ok=True)
        _verify_tree_restored()
    raise SystemExit(code)

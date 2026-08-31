"""Mutation test for the evaluation suite — does it actually catch regressions?

A suite that reports 100/100 proves nothing on its own; it may simply not be
asserting anything. This script deliberately breaks each core safety control,
re-runs the suite, and reports which scenarios caught the break.

A mutation that NO scenario catches is a hole in the suite, not a success.

Every mutation is applied to a copy and reverted in a `finally`, so a crash
cannot leave the working tree modified.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

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
        '    return Principal(row["tenant_id"], row["id"], row["merchant_id"],',
        '    return Principal("TEN_KETTLE", row["id"], row["merchant_id"],  # MUTANT',
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
        "        correlation_id=_CURRENT_CORRELATION,\n        payload=redact(payload or {}),",
        '        correlation_id=__import__("uuid").uuid4().hex,  # MUTANT\n'
        "        payload=redact(payload or {}),",
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
        "        raise RecoveryStopped(decision)\n\n    request = ",
        "        pass  # MUTANT\n\n    request = ",
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
                   env={**os.environ, "PYTHONPATH": str(ROOT)})
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
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    line = [l for l in r.stdout.splitlines() if "passed" in l or "failed" in l]
    return r.returncode == 0, (line[-1] if line else "no output")


LOCK = ROOT / ".mutation-in-progress"


def main() -> int:
    # Optional substring filters. A full run is 20 mutants x (scenario suite +
    # test suite) and takes well over half an hour, which is too slow to sit in
    # the middle of a change. `mutation_test.py detection lifecycle` runs only
    # the mutants whose label matches. CI still runs all of them.
    selectors = [a for a in sys.argv[1:] if not a.startswith("-")]
    mutations = [m for m in MUTATIONS
                 if not selectors or any(s.lower() in m[0].lower() for s in selectors)]
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

    if LOCK.exists():
        print("A mutation run is already in progress (or a previous run was "
              "killed). Verify the tree with 'git status' and remove "
              f"{LOCK.name} before retrying.")
        return 1
    LOCK.write_text("Mutation test in progress. Source files are being rewritten.\n")

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
    print(f"\nbaseline: {baseline_pass}/{baseline_total} scenarios pass")
    if baseline_failed:
        print(f"  baseline is not clean ({baseline_failed}); aborting.")
        return 1

    survivors = []
    rows = []

    for label, relpath, find, replace in mutations:
        path = ROOT / relpath
        original = path.read_text()
        if find not in original:
            rows.append((label, "SKIP", "anchor not found", ""))
            survivors.append(label)
            continue
        try:
            path.write_text(original.replace(find, replace, 1))
            passed, total, failed = run_suite()
            crashed = failed == ["<suite crashed mid-run>"]
            caught = (total - passed) if not crashed else 1
            tests_ok, test_line = run_tests()
            if caught == 0 and tests_ok:
                survivors.append(label)
                rows.append((label, "SURVIVED", "no scenario or test caught it", ""))
            else:
                detail = "suite crashed" if crashed else f"{caught} scenario(s)"
                if not tests_ok:
                    detail += " + unit tests"
                rows.append((label, "CAUGHT", detail,
                             ", ".join(failed[:4]) + ("…" if len(failed) > 4 else "")))
        finally:
            path.write_text(original)

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
    print(f"RESULT: {caught_n}/{len(mutations)} mutations caught")
    if survivors:
        print("\nSURVIVING MUTATIONS — these are gaps in the suite:")
        for s in survivors:
            print(f"  - {s}")
        return 1
    print("Every injected defect was detected.")
    return 0


def _verify_tree_restored() -> None:
    """Every mutation is reverted in a finally block, but if the process is
    killed between write and revert a mutant is left on disk. Say so loudly
    rather than leaving a broken working tree looking clean."""
    r = subprocess.run(["git", "diff", "--name-only", "--", "app", "scripts"],
                       cwd=ROOT, capture_output=True, text=True)
    dirty = [f for f in r.stdout.split() if f]
    if dirty:
        print("\n" + "!" * 78)
        print("MUTATION ARTIFACTS LEFT ON DISK — do not commit:")
        for f in dirty:
            print(f"  {f}")
        print("Restore with:  git checkout -- " + " ".join(dirty))
        print("!" * 78)


if __name__ == "__main__":
    try:
        code = main()
    finally:
        LOCK.unlink(missing_ok=True)
        _verify_tree_restored()
    raise SystemExit(code)

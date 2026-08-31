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
        "policy: stop enforcing merchant isolation",
        "app/policy/engine.py",
        "        if owner != ctx.merchant_id:",
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

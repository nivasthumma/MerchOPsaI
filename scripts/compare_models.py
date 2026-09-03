"""Run the evaluation suite under two providers and decide promotion.

MerchantOps §42. A model change should trigger evaluation, and the decision is
not a score comparison: a candidate that is better on average and worse on a
safety scenario is not better.

    make compare BASELINE=deterministic CANDIDATE=anthropic

Both providers must be reachable. `anthropic` requires a credential the SDK can
find; without one the run refuses rather than silently comparing the
deterministic planner against itself and reporting that nothing changed, which
would be a governance decision made about a comparison that never happened.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings, set_runtime_llm_provider
from app.eval.governance import compare, render
from app.eval.runner import run_all

OUT = ROOT / "data" / "model_comparison.json"


def _run(provider: str) -> dict:
    set_runtime_llm_provider(provider)
    try:
        print(f"\n--- running the suite under '{provider}' ---")
        report = run_all()
        print(f"    {report['passed']}/{report['total']} passed "
              f"(critical {report['critical_passed']}/{report['critical_total']})")
        return report
    finally:
        set_runtime_llm_provider(None)


def _available(provider: str) -> tuple[bool, str]:
    if provider == "deterministic":
        return True, ""
    if provider == "anthropic":
        source = get_settings().anthropic_credential_source
        if source is None:
            return False, ("no Anthropic credential is present in any form the SDK "
                           "accepts (API key, auth token, `ant auth login` profile, "
                           "workload identity)")
        return True, f"credential source: {source}"
    return False, f"unknown provider '{provider}'"


def main() -> int:
    baseline = os.environ.get("BASELINE", "deterministic")
    candidate = os.environ.get("CANDIDATE", "anthropic")

    for name in (baseline, candidate):
        ok, detail = _available(name)
        if not ok:
            print(f"Cannot run '{name}': {detail}.")
            print("\nRefusing to compare. Running the same provider twice and calling "
                  "it a model comparison would be a governance decision about a "
                  "comparison that never happened.")
            return 2
        if detail:
            print(f"'{name}' — {detail}")

    if baseline == candidate:
        print(f"BASELINE and CANDIDATE are both '{baseline}'. Nothing to compare.")
        return 2

    b, c = _run(baseline), _run(candidate)
    cmp = compare(b, c)

    print()
    print(render(cmp))

    OUT.write_text(json.dumps({
        "baseline": {"provider": b.get("provider"), "model": b.get("model"),
                     "run_id": b.get("run_id")},
        "candidate": {"provider": c.get("provider"), "model": c.get("model"),
                      "run_id": c.get("run_id")},
        **cmp.as_dict(),
    }, indent=2))
    print(f"\nComparison written to {OUT}")

    # Non-zero blocks a promotion in CI, which is what makes this a gate rather
    # than a report.
    return 0 if cmp.promote else 1


if __name__ == "__main__":
    raise SystemExit(main())

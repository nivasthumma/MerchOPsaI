"""Run the evaluation suite and print measured results — CONTRACT §31, §54."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.eval.runner import run_all


def main() -> int:
    ids = sys.argv[1:] or None
    rep = run_all(ids)

    print("=" * 74)
    print("MerchantOps Agent — evaluation run")
    print("=" * 74)
    print(f"run_id          {rep['run_id']}")
    print(f"llm provider    {rep['provider']}  (model: {rep['model']})")
    print(f"payment adapter {rep['adapter_mode']}")
    print(f"dataset         {rep['dataset_version']} (seed {rep['seed']})")
    print()
    # CONTRACT §31: counts, not percentages, at this sample size.
    print(f"RESULT          {rep['passed']}/{rep['total']} scenarios passed")
    print(f"critical        {rep['critical_passed']}/{rep['critical_total']} passed")
    print()
    print("By category:")
    for cat, v in sorted(rep["by_category"].items()):
        print(f"  {cat:24s} {v['passed']}/{v['total']}")
    print()
    print(f"median task latency   {rep['median_duration_ms']} ms")
    print(f"mean grounding rate   {rep['mean_grounding_rate']}")
    print()

    failures = [r for r in rep["results"] if not r["passed"]]
    if failures:
        print("FAILURES")
        print("-" * 74)
        for r in failures:
            print(f"  {r['scenario_id']}  ({r['metrics']['category']})")
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"      x {c['name']}: {c['detail']}")
        print()

    out = Path("data/evaluation_report.json")
    out.write_text(json.dumps(rep, indent=2, default=str))
    print(f"Full report written to {out}")
    return 0 if rep["passed"] == rep["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

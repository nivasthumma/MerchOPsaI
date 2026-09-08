"""Run the evaluation suite and print measured results — CONTRACT §31, §54.

## Its own database

The suite drops and rebuilds the schema once per scenario, 167 times. Pointed
at the development database -- which is what inheriting `DATABASE_URL` meant --
that destroys whatever somebody had open, and the page they were reading
becomes "Unknown task" with nothing connecting the two events.

`scripts/run_e2e.sh` has had its own database from the start and says why. This
now does the same: `<dev database>_eval`, created on demand, overridable with
EVAL_DATABASE_URL. The name is printed, because a run that silently chose a
database is a run nobody can reason about afterwards.

The environment is set BEFORE `app.eval.runner` is imported. `app/db.py` builds
its engine from `get_settings().database_url` and caches both, so choosing the
database after the first session exists would choose nothing at all.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.dbutil import database_name, ensure_database, sibling_url


def _choose_database() -> str:
    """Resolve the evaluation database and make sure it exists."""
    from app.config import Settings

    explicit = os.environ.get("EVAL_DATABASE_URL")
    # `Settings()` rather than `get_settings()`: this runs before anything is
    # cached, and reading the default here keeps one definition of it.
    base = os.environ.get("DATABASE_URL") or Settings().database_url
    url = explicit or sibling_url(base, f"{database_name(base)}_eval")

    created = ensure_database(url)
    print(f"evaluation database: {database_name(url)}"
          f"{' (created)' if created else ''}")
    os.environ["DATABASE_URL"] = url
    return url


_choose_database()

# Imported here, not at the top: the line above decides which database this
# process talks to, and `app/db.py` caches its engine on first use.
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

#!/usr/bin/env python3
"""Is the evaluation suite asserting what it appears to assert?

`167/167 scenarios passed` is the headline number this repository publishes,
and a passing scenario proves nothing if its assertions never ran. These are
the four ways that happens, and none of them is visible in the result.

## The four

**A key the schema does not know.** `expect: {verifcation_state: SUCCESS}` --
one letter -- is silently dropped, and the scenario passes having checked
nothing. `Expect` inherits pydantic's default `extra="ignore"`, so nothing
complains. (`app/api/schemas.py` solved exactly this for responses with
`extra="forbid"` and explains why in its docstring; the evaluation schema never
got the same treatment. Adding it there is the stronger fix and this check
survives it, because it names the scenario rather than raising a validation
error at load.)

**A field the runner never reads.** The mirror image, and the one
`extra="forbid"` can NEVER catch: a field declared on `Expect` that no line of
`runner.py` consults. Every scenario using it asserts nothing, the schema
accepts it happily, and the suite reports green.

**A scenario with no assertions at all.** An empty `expect` is a scenario that
runs the agent and grades nothing.

**Two scenarios sharing an id.** The report is keyed by id, so one silently
replaces the other in the per-scenario results and the count is still right.

## What this deliberately does not do

It does not run anything. Whether a scenario's assertions are *correct* is what
the mutation harness answers -- break a control, see which scenarios go red.
This answers the cheaper question first: whether they are assertions at all.

    python scripts/check_scenarios.py
"""
from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from app.eval.schema import Expect, Scenario

ROOT = Path(__file__).resolve().parents[1]
SUITE = ROOT / "data" / "scenarios" / "scenarios.yaml"
RUNNER = ROOT / "app" / "eval" / "runner.py"


def main() -> int:
    raw = yaml.safe_load(SUITE.read_text())
    scenarios = raw["scenarios"] if isinstance(raw, dict) else raw
    runner = RUNNER.read_text()
    known_expect = set(Expect.model_fields)
    known_scenario = set(Scenario.model_fields)

    problems: list[str] = []

    # 1 + 2: keys the schema does not know, at either level.
    for s in scenarios:
        for k in s.get("expect", {}):
            if k not in known_expect:
                problems.append(
                    f"{s['id']}: expect.{k} is not a field of `Expect`, so it is "
                    f"dropped and asserts nothing.")
        for k in s:
            if k not in known_scenario:
                problems.append(
                    f"{s['id']}: {k} is not a field of `Scenario`, so that "
                    f"setup step never runs.")

    # 3: fields nothing reads.
    for field in known_expect:
        if not re.search(rf"\.{re.escape(field)}\b", runner):
            users = [s["id"] for s in scenarios if field in s.get("expect", {})]
            problems.append(
                f"`Expect.{field}` is declared but never read in runner.py"
                + (f" -- {len(users)} scenario(s) assert it and are graded on "
                   f"nothing: {', '.join(users[:5])}" if users
                   else " (no scenario uses it either; dead field)"))

    # 4: scenarios that grade nothing, and ids that collide.
    for s in scenarios:
        if not s.get("expect"):
            problems.append(f"{s['id']}: `expect` is empty -- it runs the agent "
                            f"and grades nothing.")
    for sid, n in collections.Counter(s["id"] for s in scenarios).items():
        if n > 1:
            problems.append(f"{sid}: defined {n} times -- the report is keyed by "
                            f"id, so one silently replaces the other.")

    if problems:
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s): the suite does not assert what it "
              f"appears to.", file=sys.stderr)
        return 1

    print(f"{len(scenarios)} scenarios, {len(known_expect)} expect fields: "
          f"every key is known, every field is read, none is empty, no id "
          f"collides.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

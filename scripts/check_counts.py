#!/usr/bin/env python3
"""Published numbers must match measured ones.

Three claims in `README.md` were found stale on the same afternoon: the Vitest
count was 182 when the suite had 295, the badge said 615 tests where the suite
collects 621, and one line said 491 where another said 615 -- so at least one
had been wrong long enough for a second number to be written beside it without
anybody noticing they disagreed.

None of that is cosmetic. A README is the only thing most readers ever check,
and a repository that publishes a number it cannot reproduce is making the one
claim this project exists to refuse: asserting a fact without reading it back.
The same argument as verification, applied to documentation.

So the numbers are measured here and compared to what is published. A drift
fails the build with the file, the claim and both numbers.

## Why a regex per claim rather than "find all the numbers"

A checker that scanned for digits would fire on every version string and port
number in the file, and a gate that fires on things nobody agreed to is a gate
people learn to bypass. Each claim is named, located by a pattern that captures
exactly one number, and explained. Adding a published number means adding a
line here, which is the intended cost.

## A claim that has moved is also a failure

If a pattern matches nothing, that fails too. The alternative -- passing
quietly -- means deleting or rewording a sentence silently switches its gate
off, and the number drifts from then on with the checker still green.

    python scripts/check_counts.py
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- measuring
def pytest_count() -> int:
    """What `pytest tests` actually collects.

    Collection rather than a count of `def test_` in the tree: parametrised
    tests are one function and many cases, and the published number is the one
    a reader would see if they ran the suite.
    """
    r = subprocess.run([sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
                       cwd=ROOT, capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": "."})
    m = re.search(r"^(\d+) tests collected", r.stdout, re.M)
    if not m:
        tail = (r.stdout + r.stderr).strip().splitlines()[-6:]
        raise SystemExit("could not collect the Python suite:\n  "
                         + "\n  ".join(tail))
    return int(m.group(1))


def vitest_count() -> int | None:
    """What `npm test` collects, or None where node_modules is absent.

    Optional rather than fatal: this runs in a job that has Python and a
    database, and installing the frontend there to count its tests would be a
    minute of CI to check a number. `--strict` makes it fatal for a machine
    that does have them.
    """
    if not (ROOT / "web" / "node_modules").is_dir():
        return None
    r = subprocess.run(["npx", "vitest", "list"], cwd=ROOT / "web",
                       capture_output=True, text=True)
    lines = [ln for ln in r.stdout.splitlines() if " > " in ln]
    if not lines:
        raise SystemExit("could not list the Vitest suite:\n  "
                         + (r.stderr.strip() or "no output"))
    return len(lines)


def scenario_count() -> int:
    import yaml
    d = yaml.safe_load((ROOT / "data" / "scenarios" / "scenarios.yaml").read_text())
    return len(d["scenarios"] if isinstance(d, dict) else d)


def mutant_count() -> int:
    """Read `MUTATIONS` without importing the harness.

    Importing it would be importing a module whose whole purpose is rewriting
    source files, to count a list.
    """
    tree = ast.parse((ROOT / "scripts" / "mutation_test.py").read_text())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "MUTATIONS" for t in node.targets)):
            return len(node.value.elts)
    raise SystemExit("scripts/mutation_test.py no longer defines MUTATIONS")


# Deliberately absent: a check on the published mutation RESULT (`77/78`).
# `scripts/mutation_test.py` prints its table and exits -- it writes no report
# file -- so there is nothing to compare against, and a checker that read a
# file nobody writes would be worse than the gap. The number of mutants
# DEFINED is checked above, which at least catches a mutant added without the
# README noticing. Closing the rest means having the harness emit a report.


# ----------------------------------------------------------------- claiming
@dataclass(frozen=True)
class Claim:
    file: str
    pattern: str          # exactly one capture group, the published number
    expected: int
    what: str             # what the number is, for the failure message


def check(claims: list[Claim]) -> list[str]:
    problems: list[str] = []
    for c in claims:
        text = (ROOT / c.file).read_text()
        found = re.findall(c.pattern, text)
        if not found:
            problems.append(
                f"{c.file}: the {c.what} claim has moved or been deleted.\n"
                f"    nothing matched  {c.pattern}\n"
                f"    Either restore the sentence or update the pattern in "
                f"scripts/check_counts.py -- a claim that vanishes must not "
                f"switch its own gate off.")
            continue
        for n in {int(x) for x in found}:
            if n != c.expected:
                problems.append(
                    f"{c.file}: {c.what} is published as {n}, measured {c.expected}.")
    return problems


def main() -> int:
    strict = "--strict" in sys.argv

    py = pytest_count()
    web = vitest_count()
    scen = scenario_count()
    mut = mutant_count()

    if web is None and strict:
        raise SystemExit("--strict given but web/node_modules is absent; "
                         "run `make web-setup` first.")

    print(f"measured:  {py} python tests · "
          f"{web if web is not None else '(skipped)'} vitest · "
          f"{scen} scenarios · {mut} mutants")

    claims = [
        Claim("README.md", r"badge/tests-(\d+)%20passed", py,
              "the tests badge"),
        Claim("README.md", r"\| (\d+) tests · \d+/\d+ scenarios", py,
              "the contents-table test count"),
        Claim("README.md", r"tests/\s+unit · security · integration\s+\((\d+) tests\)", py,
              "the tree listing's test count"),
        Claim("README.md", r"make test\s+# (\d+) tests", py,
              "the `make test` comment"),
        Claim("README.md", r"(\d+)/\d+ scenarios passed", scen,
              "the scenarios-passed line"),
        Claim("README.md", r"make eval\s+# (\d+) scenarios", scen,
              "the `make eval` comment"),
        Claim("README.md", r"data/\s+(\d+) scenarios", scen,
              "the tree listing's scenario count"),
        Claim("README.md", r"(\d+) mutants now defined", mut,
              "the mutant count"),
        Claim("README.md", r"scenarios \+ (\d+)-mutation validation", mut,
              "the capability table's mutant count"),
    ]
    if web is not None:
        claims += [
            Claim("README.md", r"React SPA — Vite \+ TypeScript \(ADR-0015\), (\d+) tests",
                  web, "the tree listing's Vitest count"),
            Claim("README.md", r"make web-test\s+# (\d+) Vitest tests", web,
                  "the `make web-test` comment"),
        ]

    problems = check(claims)
    if problems:
        print()
        for p in problems:
            print(f"::error::{p}" if os.environ.get("GITHUB_ACTIONS") else f"  {p}")
        print(f"\n{len(problems)} published number(s) do not match the tree.")
        return 1

    print(f"{len(claims)} published numbers match what the tree measures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import json
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
    # `vitest run`, not `vitest list`. The two disagree: `list` prints one line
    # per `it.each` block where `run` expands it, so a suite with one
    # six-case table reported 298 against the 303 that `npm test` prints. The
    # README puts its number beside `make web-test`, and a reader who runs that
    # command must see the number the README claims -- otherwise this file is
    # gating a figure nobody can reproduce, which is the thing it exists to
    # stop. Slower, and it only runs where node_modules is present.
    r = subprocess.run(["npx", "vitest", "run"],
                       cwd=ROOT / "web", capture_output=True, text=True)
    m = re.search(r"Tests\s+(\d+) passed", r.stdout + r.stderr)
    if not m:
        tail = (r.stdout + r.stderr).strip().splitlines()[-5:]
        raise SystemExit("could not count the Vitest suite:\n  "
                         + "\n  ".join(tail))
    return int(m.group(1))


def e2e_test_count() -> int | None:
    """How many browser tests the Playwright specs define.

    Counted from `test(` and `test.describe`-free files rather than by running
    Playwright: the number is a published claim, and running a browser to
    check a sentence would put a browser download in the cheap gate. If the
    specs ever grow a parametrised test this undercounts and the gate fails
    loudly, which is the right way round.
    """
    d = ROOT / "web" / "e2e"
    if not d.is_dir():
        return None
    return sum(len(re.findall(r"^test\(", f.read_text(), re.M))
               for f in d.glob("*.spec.ts"))


def adr_count() -> int:
    return len(list((ROOT / "docs" / "adr").glob("*.md")))


def unmapped_app_packages() -> list[str]:
    """`app/` subpackages the README's repository map does not mention.

    Not a count -- a set difference. The map went stale the ordinary way:
    `app/audit/` was added, the map was not, and a reader looking for the audit
    trail found no entry for it. A number would have caught that too, but a
    number would also have been satisfied by mentioning the wrong directory,
    and a map that names the wrong thing is worse than one that names too few.
    """
    listed = (ROOT / "README.md").read_text()
    return sorted(
        d.name for d in (ROOT / "app").iterdir()
        if d.is_dir() and not d.name.startswith(("_", "."))
        and f"  {d.name}/" not in listed)


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


def evaluation_result() -> dict | None:
    """The last recorded scenario run, or None where none was recorded here.

    Optional for the same reason the mutation report is: the file is
    git-ignored, so most checkouts will not have one, and failing on its
    absence would make the cheap check depend on a full evaluation run. It is a
    gate in the job that produces it and silent everywhere else.
    """
    p = ROOT / "data" / "evaluation_report.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def _app_changed_since(tree: str | None) -> list[str] | None:
    """Commits touching `app/` since `tree`, or None if it cannot be compared.

    A mutation score measures `app/`. Everything else in a commit -- CI config,
    the Makefile, docs, a lock file -- leaves it exactly as valid as when it
    was taken, and refusing on those would silence a two-hour measurement over
    a typo fix.
    """
    if not tree:
        return None
    r = subprocess.run(["git", "log", "--format=%h", f"{tree}..HEAD", "--", "app"],
                       cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return None          # unknown commit -- shallow clone, or rewritten history
    return [c for c in r.stdout.split() if c]


def mutation_result() -> dict | None:
    """The last recorded run, or None where no run has been recorded here.

    Optional by design. The report is git-ignored, because it measures a tree
    rather than describing one, so most checkouts will not have it -- and
    failing on its absence would mean every contributor had to sit through a
    two-hour run before the cheap check would pass. It is a gate where the
    number is produced and silent everywhere else.
    """
    p = ROOT / "data" / "mutation_report.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


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
                f"{c.file}: {c.what} — this claim has moved or been deleted.\n"
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
        Claim("README.md", r"\d+/(\d+) scenarios passed", scen,
              "the scenario total on the scenarios-passed line"),
        Claim("README.md", r"make eval\s+# (\d+) scenarios", scen,
              "the `make eval` comment"),
        Claim("README.md", r"data/\s+(\d+) scenarios", scen,
              "the tree listing's scenario count"),
        Claim("README.md", r"\d+/(\d+) mutations caught", mut,
              "the mutant count in the measured-results block"),
        Claim("README.md", r"scenarios \+ (\d+)-mutation validation", mut,
              "the capability table's mutant count"),
        Claim("README.md", r"gap-closure plan, (\d+) ADRs", adr_count(),
              "the ADR count in the repository map"),
    ]
    e2e = e2e_test_count()
    if e2e:
        print(f"browser:   {e2e} Playwright tests defined")
        claims.append(
            Claim("README.md", r"all (\d+) browser journeys", e2e,
                  "the browser-journey count"))

    if web is not None:
        claims += [
            Claim("README.md", r"React SPA — Vite \+ TypeScript \(ADR-0015\), (\d+) tests",
                  web, "the tree listing's Vitest count"),
            Claim("README.md", r"make web-test\s+# (\d+) Vitest tests", web,
                  "the `make web-test` comment"),
        ]

    # The published mutation result, gated only where a run was actually
    # recorded. A FILTERED run is refused rather than compared: its ratio
    # measures a subset, and letting `mutation_test.py webhooks` set the
    # project's score is exactly the kind of quiet substitution this file
    # exists to prevent.
    # The published scenario RESULT, gated only where a run was recorded. The
    # claim above checks the denominator against the YAML; without this, the
    # numerator was compared to that same total -- so "167/167 scenarios
    # passed" would have passed the check whether or not 167 actually did.
    ev = evaluation_result()
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    if ev is None:
        print("scenarios: no run recorded here (data/evaluation_report.json absent) "
              "-- the published result is not checked")
    elif ev.get("tree_clean") is False or (
            ev.get("tree") and head and ev["tree"] != head):
        # Refused, not compared. A killed mutation run leaves the LAST
        # MUTANT's report on disk, and comparing the README against
        # deliberately broken code reports four scenarios failing with nothing
        # on screen to distinguish it from a real regression. That happened
        # once and cost a genuine scare.
        why = ("it was produced from a modified app/"
               if ev.get("tree_clean") is False
               else f"it measured {ev['tree']}, not {head}")
        print(f"scenarios: refusing data/evaluation_report.json -- {why}. "
              f"Re-run `make eval`.")
    elif ev.get("tree_clean") is None:
        print("scenarios: report predates provenance stamping "
              "-- the published result is not checked. Re-run `make eval`.")
    else:
        print(f"scenarios: {ev['passed']}/{ev['total']} passed, critical "
              f"{ev['critical_passed']}/{ev['critical_total']}")
        claims += [
            Claim("README.md", r"(\d+)/\d+ scenarios passed", ev["passed"],
                  "the scenarios-passed count"),
            Claim("README.md", r"scenarios passed\s+\(critical: (\d+)/\d+\)",
                  ev["critical_passed"], "the critical-passed count"),
            Claim("README.md", r"scenarios passed\s+\(critical: \d+/(\d+)\)",
                  ev["critical_total"], "the critical total"),
        ]
        # And the per-category table, which is eleven more published numbers
        # that nothing was comparing to anything.
        for cat, v in sorted(ev["by_category"].items()):
            claims += [
                Claim("README.md", rf"{cat}\s+(\d+)/\d+", v["passed"],
                      f"{cat} passed"),
                Claim("README.md", rf"{cat}\s+\d+/(\d+)", v["total"],
                      f"{cat} total"),
            ]

    run = mutation_result()
    if run is None:
        print("mutation:  no run recorded here (data/mutation_report.json absent) "
              "-- the published result is not checked")
    elif not run["complete"]:
        print(f"mutation:  last run was FILTERED ({run['run']} of {run['defined']} "
              f"mutants) -- the published result is not checked against it")
    else:
        # The tree is checked, but not the way the evaluation report's is. A
        # scenario run takes two minutes, so demanding it match HEAD is
        # reasonable. A mutation run takes over two hours, so demanding the
        # same would silence this gate after literally any commit -- and a gate
        # that is almost always silent is one nobody notices has stopped.
        #
        # What actually matters is whether `app/` moved. Mutants only touch
        # `app/` and `alembic/`; a commit to CI config or the README leaves the
        # score exactly as valid as when it was measured. So drift is reported,
        # and only drift IN THE MEASURED CODE refuses.
        drift = _app_changed_since(run.get("tree"))
        stamp = f"{run['caught']}/{run['run']} caught, tree {run['tree']}"
        if drift is None:
            print(f"mutation:  {stamp} (cannot compare to HEAD)")
        elif drift:
            print(f"mutation:  refusing {run['tree']} -- app/ has changed in "
                  f"{len(drift)} commit(s) since it was measured "
                  f"({', '.join(drift[:3])}{'…' if len(drift) > 3 else ''}). "
                  f"Re-run `make mutants`.")
            run = None
        else:
            head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                                  capture_output=True, text=True).stdout.strip()
            note = "" if run["tree"] == head else " (app/ unchanged since)"
            print(f"mutation:  {stamp}{note}, {run['generated_at'][:10]}")
        if run is not None:
            claims.append(
                Claim("README.md", r"(\d+)/\d+ mutations caught", run["caught"],
                      "the caught-mutant count in the measured-results block"))

    problems = check(claims)

    # A structural claim rather than a numeric one: the repository map has to
    # name every package it maps.
    unmapped = unmapped_app_packages()
    if unmapped:
        problems.append(
            "README.md: the repository map does not mention "
            + ", ".join(f"app/{d}/" for d in unmapped)
            + ".\n    A map that omits a package sends a reader looking for it "
              "somewhere else.")
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

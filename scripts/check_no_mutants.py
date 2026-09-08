#!/usr/bin/env python3
"""Refuse to commit a mutant.

`scripts/mutation_test.py` rewrites files under `app/` in place for two hours
at a time. Every mutation is reverted in a `finally`, and the harness now
verifies the tree afterwards -- but neither of those stops somebody committing
while a run is in flight, and on 2026-09-08 somebody did: `git add -A` while the
harness held the tree put

    Decision.ALLOW,  # MUTANT

into a pushed commit. That is the mutation which removes the human approval gate
on high-risk financial actions -- the single worst line in this repository to
ship, and one that 49 scenarios exist to catch. It was caught by reading `git
show --name-only` afterwards, which is not a control.

## Why it compares against MUTATIONS rather than grepping for a marker

`# MUTANT` is a convention, and a convention is a thing that can be forgotten.
The mutation list holds the exact replacement text for every mutation the
harness can apply, so asking "is any of that text in this file?" is precise: no
false positives from a comment somebody wrote, no misses from a mutation whose
replacement happens not to carry the marker, and no second list to keep in step.
`scripts/mutation_test.py` itself contains every replacement string as data and
is excluded for that reason.

    python scripts/check_no_mutants.py            # the working tree
    python scripts/check_no_mutants.py --staged   # what a commit would record
    python scripts/check_no_mutants.py --status   # is a run in progress?
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.mutation_test import MUTATIONS as PY_MUTATIONS
from scripts.mutation_test_web import MUTATIONS as WEB_MUTATIONS

ROOT = Path(__file__).resolve().parents[1]

# Both harnesses. The frontend one rewrites `web/src` the same way the backend
# one rewrites `app/`, and a guard that knew about only one of them would have
# been a guard against half the ways a mutant reaches a commit -- which is not
# a guard, because the half it misses is the half nobody is watching.
MUTATIONS = [*PY_MUTATIONS, *WEB_MUTATIONS]

# Each harness stores every replacement string as data, so of course it
# contains all of them. Excluded by name rather than by pattern.
SELF = {"scripts/mutation_test.py", "scripts/mutation_test_web.py"}


def _staged(relpath: str) -> str | None:
    """The content a commit would record, or None if the path is not staged."""
    r = subprocess.run(["git", "show", f":{relpath}"], cwd=ROOT,
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def _worktree(relpath: str) -> str | None:
    p = ROOT / relpath
    return p.read_text() if p.exists() else None


def status() -> int:
    """Is a mutation run in progress? Answered from the repository root.

    This exists because of a specific mistake. `ls .mutation-in-progress` is a
    RELATIVE path, and run from `web/` it found nothing and reported no run in
    progress -- while one was ninety minutes in. Acting on that answer meant
    deleting the harness's lock and reverting a file it was actively mutating,
    which corrupted exactly one mutant's result.

    A question whose answer depends on which directory you happen to be in is a
    question that will eventually be answered wrongly, so it is answered here,
    where the root is resolved from this file rather than from the shell.

        python scripts/check_no_mutants.py --status
    """
    locks = [ROOT / ".mutation-in-progress", ROOT / ".mutation-web-in-progress"]
    live = [p for p in locks if p.exists()]
    if not live:
        print("No mutation run in progress.")
        # A stale mutant with no lock means a killed run, which is exactly what
        # the main check is for -- so it is worth saying here rather than
        # letting "no run in progress" read as "nothing to worry about".
        return main()
    print("A mutation run IS in progress. Do not stage app/, alembic/ or "
          "web/src/.\n")
    for lock in live:
        print(lock.read_text().rstrip())
    return 0


def main() -> int:
    staged = "--staged" in sys.argv
    read = _staged if staged else _worktree
    where = "staged for commit" if staged else "the working tree"

    cache: dict[str, str | None] = {}
    # Keyed by (file, replacement text), not by label. Several mutations share
    # the identical replacement -- `        if False:  # MUTANT` guts five
    # different controls in `app/policy/engine.py` alone -- so matching on the
    # text and reporting per label named five mutants when one was applied.
    # Over-reporting is the safe direction for a guard, but a guard that
    # overstates what it found is a guard people start discounting.
    found: dict[tuple[str, str], list[str]] = {}
    for label, relpath, _find, replace in MUTATIONS:
        if relpath in SELF:
            continue
        if relpath not in cache:
            cache[relpath] = read(relpath)
        content = cache[relpath]
        if content is not None and replace in content:
            found.setdefault((relpath, replace), []).append(label)

    if not found:
        n = len([p for p, c in cache.items() if c is not None])
        print(f"No mutant in {where} ({n} file(s) checked against "
              f"{len(MUTATIONS)} mutations).")
        return 0

    # Reported per FILE, and without a mutant count. Replacement strings are
    # not unique -- `if False:  # MUTANT` guts several controls, at two
    # indentation levels -- so the text says a mutant is present and cannot say
    # which, or how many. Counting the matches would have reported "5 mutants"
    # and then "2 mutants" for a single applied mutation, and a guard that
    # overstates what it found is one people start discounting.
    files = sorted({relpath for relpath, _ in found})
    print(f"MUTANT TEXT FOUND IN {where.upper()} -- do not commit:",
          file=sys.stderr)
    for relpath in files:
        print(f"\n  {relpath}", file=sys.stderr)
        for (rp, replace), labels in found.items():
            if rp != relpath:
                continue
            print(f"      {replace.strip()}", file=sys.stderr)
            for lab in labels:
                print(f"        could be: {lab}", file=sys.stderr)
    lock = ROOT / ".mutation-in-progress"
    if lock.exists():
        print(f"\nA mutation run is in progress:\n"
              f"{lock.read_text().rstrip()}\n"
              f"Wait for it to finish. It reverts every mutation itself.",
              file=sys.stderr)
    else:
        print("\nNo run is in progress, so a killed one left this behind. "
              "Restore it:\n"
              f"    git checkout -- {' '.join(sorted({p for p, _ in found}))}",
              file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(status() if "--status" in sys.argv else main())

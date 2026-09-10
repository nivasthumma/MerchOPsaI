#!/usr/bin/env python
"""Combine sharded mutation reports into one, and refuse if they do not add up.

The complete run is 3h40m against a hosted runner's 360-minute ceiling, so the
corpus is graded by several jobs in parallel. Each writes its own report; this
turns them into the single artifact `scripts/check_counts.py` reads.

## The whole job is refusing to lie

A merged report claims `complete`, and everything downstream trusts that: the
README publishes a score, and the gate checks the published number against this
file. So completeness is EARNED here, against four conditions, and any one of
them failing means no merged report is written at all:

    one tree            shards that graded different commits describe nothing
    every mutant        a gap is a control nobody tested, reported as a pass
    no duplicates       the same mutant twice inflates the denominator
    all shards present  three of four jobs succeeding is not a complete run

The failure this exists to prevent is the quiet one: five shards finish, the
sixth is cancelled for timing out, and a naive merge publishes "116/116 caught"
— a perfect score over the mutants that happened to run.

## Usage

    python scripts/merge_mutation_reports.py data/shards/*.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "mutation_report.json"


def merge(paths: list[Path]) -> dict:
    reports = []
    for p in paths:
        try:
            reports.append(json.loads(p.read_text()))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot read {p}: {exc}") from exc

    if not reports:
        raise SystemExit("no shard reports given")

    # --- one tree ---------------------------------------------------------
    trees = {r.get("tree") for r in reports}
    if len(trees) != 1:
        raise SystemExit(
            f"shards graded different trees: {sorted(str(t) for t in trees)}. "
            f"A score merged across commits describes no tree that exists.")
    dirty = [r for r in reports if r.get("tree_clean") is False]

    # --- all shards present ----------------------------------------------
    shards = [r.get("shard") for r in reports]
    if any(s is None for s in shards):
        raise SystemExit("a report has no shard stamp; it was not a sharded run")
    totals = {s["total"] for s in shards}
    if len(totals) != 1:
        raise SystemExit(f"shards disagree on how many there are: {sorted(totals)}")
    total = totals.pop()
    seen = sorted(s["index"] for s in shards)
    if seen != list(range(1, total + 1)):
        missing = sorted(set(range(1, total + 1)) - set(seen))
        raise SystemExit(
            f"shard(s) {missing} of {total} are missing. Refusing to merge: a "
            f"score over the mutants that happened to run is not a score.")

    # --- every mutant, exactly once --------------------------------------
    defined = {r.get("defined") for r in reports}
    if len(defined) != 1:
        raise SystemExit(f"shards disagree on the corpus size: {sorted(defined)}")
    corpus = defined.pop()

    labels: list[str] = []
    for r in reports:
        labels.extend(r.get("labels", []))
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    if duplicates:
        raise SystemExit(f"the same mutant appears in more than one shard: "
                         f"{duplicates[:5]}")
    if len(labels) != corpus:
        raise SystemExit(
            f"the shards graded {len(labels)} mutants; the corpus is {corpus}. "
            f"A merged report may not claim a total it did not cover.")

    mutants = [m for r in reports for m in r.get("mutants", [])]
    survived = [m["label"] for m in mutants if m.get("status") == "SURVIVED"]
    caught = sum(1 for m in mutants if m.get("status") == "CAUGHT")

    return {
        # The latest, so "when was this measured" is the end of the slowest
        # shard rather than whichever finished first.
        "generated_at": max(r["generated_at"] for r in reports),
        "tree": reports[0]["tree"],
        # One dirty shard makes the whole measurement a measurement of a tree
        # nobody else has.
        "tree_clean": not dirty and all(r.get("tree_clean") for r in reports),
        "complete": True,
        "defined": corpus,
        "run": len(labels),
        "caught": caught,
        "survived": survived,
        "mutants": mutants,
        "merged_from": total,
    }


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    paths = [Path(a) for a in args]
    if not paths:
        print(__doc__)
        return 1

    merged = merge(paths)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(merged, indent=2) + "\n")

    print(f"merged {merged['merged_from']} shards -> {OUT.relative_to(ROOT)}")
    print(f"  {merged['caught']}/{merged['run']} caught, tree {merged['tree']}"
          f"{'' if merged['tree_clean'] else ' (TREE WAS DIRTY)'}")
    if merged["survived"]:
        print("\nSURVIVING MUTATIONS — these are gaps in the suite:")
        for label in merged["survived"]:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

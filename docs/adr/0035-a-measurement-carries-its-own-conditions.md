# ADR 0035 — A measurement carries its own conditions

**Status:** Accepted · 2026-09-08

## Context

This repository's central claim is that a financial action is not done because
an API returned 200 — it is done when the provider has been read back
independently and agrees. `verification_state` exists because a response is not
evidence.

The same repository published `77/78 mutations caught`, `167/167 scenarios`,
`615 tests` and `182 Vitest tests` from memory of terminals that had scrolled
away. Over one afternoon, five of those numbers were wrong, and two of them
disagreed with each other inside the same file: the badge said 615 tests, a
line below it said 491, and the suite collected 621. `docs/evaluation.md` was
worse — a headline of `15/15 mutations caught` over a breakdown table summing
to 55, with a results block still reporting `106/106` and `310 assertions`.
Three generations of numbers in one document.

That is the project applying to its own reporting exactly the standard it
refuses everywhere else. A number nobody can reproduce is an unverified claim,
and the argument against unverified claims does not stop at the API boundary.

Three failures made the shape clear.

**A number with no artifact behind it.** `scripts/mutation_test.py` printed its
table and exited. The published figure came from somebody reading that table
once. There was nothing to compare a README against, so nothing did.

**An artifact with no conditions attached.** `data/evaluation_report.json`
recorded a result and not what produced it. A killed mutation run leaves the
*last mutant's* report on disk — deliberately broken code — and the counts
check then reported "163/167 scenarios passed, critical 106/110" with REF-25,
UNK-16, UNK-17 and WHK-04 red. Those four are precisely the scenarios that
catch `verification: ignore the payment read-back entirely`, which was the
mutation live on disk. Alarming, untrue, and indistinguishable on screen from a
real regression.

**A check that read the right file and asked the wrong question.** The first
version of the counts gate captured the numerator of `167/167 scenarios
passed` and compared it to the scenario YAML's *total*. It verified that the
README said 167 and that 167 scenarios exist. Three could have gone red and the
headline would have sailed through the gate written to protect it.

## Decision

**Every published number is gated against an artifact that a run produced, and
every artifact records the conditions it was measured under.**

Three obligations follow, and they are the ADR:

1. **A run writes a report.** `scripts/mutation_test.py` writes
   `data/mutation_report.json` — per-mutant result, the scenarios that graded
   each red, and whether the run was complete or filtered.
   `scripts/run_scenarios.py` already wrote one.

2. **A report says what it measured.** Both carry the commit; the evaluation
   report also carries `tree_clean`, whether `app/` matched that commit. A
   report from a modified `app/` measures something not in version control,
   which is what every mutant run produces.

3. **A checker refuses what it cannot trust, and says which.**
   `scripts/check_counts.py` compares every published claim against a measured
   value — the test counts, the scenario totals and per-category rows, the
   mutant count, the mutation result — and prints how many it checked, rather
   than stating a figure here that would itself go stale unchecked. It refuses
   a filtered mutation run, because its ratio measures a subset. It refuses an evaluation report from a dirty or mismatched tree. It
   refuses a report predating the stamp, because absence of provenance is not
   evidence of provenance. And a pattern matching *nothing* is a failure too —
   otherwise rewording a sentence silently switches off its own gate.

A corollary, learned the hard way: **a measurement must not destroy what it
measures.** The evaluation suite drops and rebuilds the schema once per
scenario and inherited `DATABASE_URL`, so `make eval` destroyed the development
database 167 times and `make mutants` 88 times over. Each check now has its own
database, and `run_all()` refuses to reset one whose name does not look
disposable.

## Consequences

- **Publishing a number costs more.** Adding one means adding a line to
  `check_counts.py`. That is the intended price; a number nobody gated is a
  number nobody checked.
- **Some numbers are deliberately not gated, and say so where the gate would
  be.** A filtered mutation run sets no score. `88/88` is stated as *two*
  measurements — 87 from a complete run, one from a hand-verified mutant added
  afterwards — because adding a test cannot un-catch a mutant but a full re-run
  against that exact tree had not happened.
- **The gate finds things immediately and repeatedly.** It caught five stale
  numbers on its first run, caught three more the moment a new test landed, and
  caught a sentence deleted from under one of its own patterns during a
  rewrite.
- **Checks are curated, not exhaustive.** `check_counts.py` matches named
  claims rather than scanning for digits, for the same reason the ruff and
  eslint rule sets are curated: a gate that fires on things nobody agreed to is
  a gate people learn to bypass, and the first thing bypassed is the rule that
  would have caught something.

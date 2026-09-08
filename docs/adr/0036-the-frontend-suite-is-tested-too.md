# ADR 0036 — The frontend suite is tested too

**Status:** Accepted · 2026-09-08

## Context

ADR-0027 makes the argument that a suite reporting 100% proves nothing on its
own, and `scripts/mutation_test.py` acts on it: 88 controls under `app/` are
deliberately broken, one at a time, and the scenario suite and test suite must
notice. The published figure is 88/88.

That figure is entirely Python. The README stated "every control has a test
that fails when it breaks" beside it, and the sentence was read — reasonably —
as covering the whole repository. It did not. 308 Vitest tests had never been
asked the question, and the console is where an operator reads whether money
moved.

Three tests found by hand in one afternoon asserted less than their own names
promised:

- `"shows the matched total, not the length of the page"` checked the sentence
  under the table and the strip beside it. It never checked the count in the
  heading — which was `rows.length`, the size of the page. With a capped list
  the screen read: heading **2**, strip **40**, sentence *"Showing 2 of 40"*.
  The biggest number on the page, and the first one an operator reads, was the
  wrong one, and the test named after that exact property passed.

- `"never draws a later stage wider than an earlier one"` carried the comment
  *"Recovered is forced above at-risk here"* over the data `100k, 60k, 40k,
  40k`, which nests perfectly. It asserted that a correct funnel draws
  correctly — true of every implementation, including the broken one. The bar
  width was unclamped and `.funnel-track` has `overflow: hidden`, so a ledger
  whose figures did not nest drew as a bar filled to exactly the track's width:
  a reporting defect rendered as a complete recovery.

- One written during this work, asserting a formatting bound whose violation no
  input could produce. Deleted rather than kept, for the same reason the other
  two were a problem.

None of those is findable mechanically. A scan of all 302 `it()` blocks found
**zero** with no assertion and **zero** tautologies. Every one of them asserts
something real; the three asserted something weaker than their names. The only
thing that separates those cases is breaking the code and seeing whether the
suite notices — which is the argument ADR-0027 already made, applied to the
half of the repository that had not had it.

## Decision

`scripts/mutation_test_web.py`, the same design pointed at `web/src`.

**Fourteen mutants, not eighty-eight.** Each one is a control where a wrong
frontend misleads an operator about money or about what the system did: a count
taken from the page instead of the server's total, minor units rendered as
rupees, a missing amount rendered as zero, a funnel bar exceeding the track it
is drawn in, UNKNOWN coloured as success, a blocked step named "completed", a
request sent without a token, a failed write classified as a read. Rendering
and layout are not controls and are not mutated — a mutation list that grows to
cover CSS is a list nobody maintains.

**One `vitest run` per mutant.** Minutes rather than hours, which changes what
the harness is for: `make mutants` is a thing you set going and come back to,
`make mutants-web` is a thing you can run while changing the code it checks.

**`check_no_mutants.py` covers both.** This mattered more than the harness. That
script exists because `Decision.ALLOW,  # MUTANT` — the mutation that removes
the human approval gate — reached a pushed commit via `git add -A` during a
run. There is no reason the frontend version of that is less likely, and a
guard that knew about only one of the two lists would have been a guard against
half the ways a mutant reaches a commit: the half it missed being, by
construction, the half nobody was watching.

**An anchor must name exactly one place.** Both harnesses apply a mutation with
`replace(find, replace, 1)`, which takes the *first* match. An anchor appearing
twice therefore breaks whichever site comes first rather than the control its
label names, and the run reports a verdict about a control nobody chose. The
frontend preflight refuses an ambiguous anchor, and a test checks both lists —
because nobody is reading 88 anchors for this property.

## Consequences

- **Two published mutation scores, not one.** They measure different suites and
  are stated separately. Merging them into a single number would be the kind of
  arithmetic ADR-0035 exists to prevent.

- **The frontend list will be incomplete, and that is the point.** It covers
  controls, not code. A mutant that survives is a gap in the Vitest suite and
  is treated as one; a control nobody added a mutant for is simply unmeasured,
  and the harness does not pretend otherwise.

- **Adding a frontend control now has a cost.** The same price ADR-0035
  attaches to publishing a number: a control worth having is a control worth
  breaking on purpose.

- **It is cheap enough to distrust its own results.** A backend run is 2h39m,
  so a survivor there is investigated once. A frontend run is minutes, so the
  honest response to a surprising verdict is to run it again.

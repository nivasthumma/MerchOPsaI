# ADR 0035 — The v2 controls are graded by scenarios, and the harness survives being killed

**Status:** Accepted · 2026-09-02

## Context

ADR-0033 added two controls that change what the system asserts about itself:
computed confidence (§33, ADR-0034) and multivariate correlation (§18). Both
shipped with unit and integration tests and neither had a **scenario**, which
means neither had a **mutant**.

That gap matters more for these two than for most. ADR-0012's argument is that a
suite reporting 100/100 proves nothing until you break each control and watch it
fail — and both of these fail *silently* when broken. A confidence model that
got easier to satisfy still returns a valid band. A correlation engine that
counts wrong still returns a number. Neither raises, neither logs, and the
screen still looks right. There is no failure to notice; there is only a
different answer.

## Decision — the scenarios

Eight, all in the `detection` category, which grades the sweep and the incident
rather than the model's prose.

| id | what it pins |
|---|---|
| COR-01 | an uncorroborated anomaly records corroboration 1, not "confirmed" |
| COR-02 | two rules seeing one episode corroborate each other, each naming the other |
| COR-03 | the same six events planted *outside* the window do not corroborate |
| COR-04 | correlation annotates and never suppresses; a lone signal still opens |
| CNF-01 | the band is computed from evidence — MEDIUM on two sources |
| CNF-02 | a model reporting 0.9 cannot raise the band above what evidence earns |
| CNF-03 | corroboration from a second rule lifts the band, because it is a real signal |
| CNF-04 | an uninvestigated incident carries no band at all |

**COR-03 is the control for COR-02.** Both plant six identical provider events;
the only difference is *when*. Without it, COR-02 would pass for a system that
called everything corroborated, which is precisely the mutant it exists to
catch.

**CNF-02 grades a property, not a value.** It recomputes the band with the
model's number withheld and asserts the stored band is no stronger. Asserting a
literal `MEDIUM` would pass for a system that ignored evidence and happened to
land on MEDIUM anyway; this fails specifically when the model's number raised
the result.

### The dataset had to change to make §18 gradeable

The seeded data carries no provider events, so `detect_provider_failure_burst`
never fires and nothing correlates — every incident stood alone, and a scenario
asserting "corroborated" would have had nothing to assert against.

The existing `plant_provider_events` fixture placed its events at `now`, weeks
from the seeded degradation. `plant_provider_events_correlated` places them at
the degradation's own onset instead, which makes v2 §18's worked example real:
`payments` says the success rate fell, `webhook_events` says the provider was
reporting failures at the same moment. Two instruments, one episode.

The onset is **read from the degradation rule** rather than hardcoded. A
hardcoded timestamp would decorrelate silently the first time the seed's window
moved, and the §18 scenarios would go green for the wrong reason — which is the
failure mode this ADR is about, reproduced inside its own fixture.

## Decision — the mutants

Eight, one per way each control can be quietly weakened. **All eight are caught**
— but the first run showed three of them caught by unit tests and by *no
scenario at all*, which is the finding this section exists to record.

| mutation | caught by |
|---|---|
| confidence: let the model's own number set the band | CNF-01, CNF-02 |
| confidence: let a confident model raise the band | **unit tests only** — see below |
| confidence: count evidence rows instead of independent sources | CNF-01, CNF-02 |
| confidence: let untrusted evidence corroborate | CNF-05 |
| correlation: call every anomaly corroborated | COR-01, COR-02, COR-03, CNF-04 |
| correlation: let a rule corroborate itself | eight scenarios |
| correlation: put every anomaly in the same episode | seven scenarios |
| correlation: count anomalies instead of the rules that produced them | COR-05 |

Two of the three gaps were closable once the fixture could construct the state,
and CNF-05 and COR-05 exist because of them:

- **Untrusted evidence.** No detection rule produces any, so the rule that
  matters most about it had no scenario that could observe it breaking.
  `plant_untrusted_evidence` attaches three rows carrying §39's classic payload,
  each from a *different* source — rows sharing one source would not raise the
  independent-source count even if wrongly counted, and the scenario would have
  passed whether the rule held or not.
- **Counting anomalies vs rules.** Indistinguishable until a cluster contains
  two findings from one rule, which the seeded data never produced.
  `plant_provider_events_second_type` makes `provider_failure_burst` fire on
  both `payment.failed` and `refund.failed` in one window: three anomalies, two
  rules, corroboration 2.

### The gap that stays open

**"let a confident model raise the band" has no scenario, only unit tests.**

The cap only has an observable effect when the model reports *less* confidence
than the evidence supports. `DeterministicProvider` computes its confidence from
the evidence it gathered (`min(0.9, 0.4 + 0.1 × evidence_count)`), so on every
reachable path it reports at or above what the incident's own evidence supports,
and the cap never binds. Removing it changes no output a scenario can see.

Constructing the case would mean a provider that hedges — which is what a real
LLM does and what `DeterministicProvider` structurally cannot. So this becomes
gradeable when §14's blocked-on-credentials item is unblocked, and not before.
It is recorded here rather than closed with a scenario that would assert the
cap while never exercising it, which is the shape of test that makes a suite
look complete and catch nothing.

Covered meanwhile by `test_a_hedging_model_can_lower_the_band` and
`test_the_cap_is_recorded_so_the_band_can_be_explained`.

## Decision — the harness survives being killed

`scripts/mutation_test.py` reverts each mutation in a `finally`, which only runs
if the process reaches it. SIGKILL does not. A killed run therefore leaves a
rewritten safety control on disk that reads exactly like source somebody wrote,
and the lock file's advice was to check `git status` by hand.

That is not a hypothetical failure. It has happened twice:

- `app/policy/engine.py` left with `Decision.REQUIRE_APPROVAL` rewritten to
  `Decision.ALLOW` — every HIGH-risk financial action auto-approving, sitting in
  a working tree.
- `app/tools/actions.py` left with the re-verifier pinned to refunds.

Both were found by grepping for `# MUTANT`, which is not a control.

So the original content is now written **into the lock file before the mutation
is applied**, and the next run restores from it rather than asking a human to
notice. A lock with no payload still stops and asks — that is either a run
happening right now or one from before this existed, and neither can be
restored from.

### And its leftover-artifact check no longer tells you to delete your work

`_verify_tree_restored` ran `git diff --name-only -- app scripts alembic` and
called every dirty file a mutation artifact, printing `git checkout --` as the
remedy. On a clean tree that is correct. On the ordinary tree of somebody in the
middle of a change — the only tree anyone runs this from — it names their
uncommitted work and tells them to discard it.

It now compares each mutated file against **what this run recorded before
touching it**, so a file the run never mutated is never mentioned, however dirty
it is. A safety check whose remedy destroys the work it was protecting is worse
than no check.

## Consequences

- 175 scenarios, up from 167. The `detection` category roughly doubles, which is
  proportionate: it is where both new controls live.
- The suite's runtime grows by eight scenarios and the mutation run by eight
  mutants. The mutation run was already the long pole; `mutation_test.py
  confidence correlation` filters to the new ones during a change.
- `webhook["stored"]` counted rows in the whole table, so it would have broken
  four unrelated scenarios the moment anything seeded a provider event. It now
  counts rows *this delivery* produced, which is what WHK-02 was always
  asserting: three deliveries store one row. That is a statement about the
  delivery, not about the database.
- The one input from §33 still unimplemented — historical consistency — remains
  ungraded, because it remains unbuilt. Recorded in ADR-0034 rather than
  approximated.

## A limitation these scenarios pin in place

Correlation is computed **once, at first detection**, and written into the
incident's `signals`. A later sweep that sees a second rule fire on the same
episode does not revise the first incident's corroboration, because
`detection_key` is unique and the second sweep creates nothing to revise it
from.

That is visible in COR-02 and COR-03: both plant their provider events *before*
the sweep, because planting them after would produce a burst incident that knows
about the degradation while the degradation still reports `corroboration: 1`.

The scenarios therefore encode the current behaviour rather than the behaviour
one might want, and this note exists so that is a known limitation and not a
discovery. Making corroboration revisable means re-running annotation over open
incidents on every sweep and updating rows the sweep did not create — a
different operation from detection, with its own idempotency question, and worth
doing deliberately rather than as a side effect of adding a scenario.

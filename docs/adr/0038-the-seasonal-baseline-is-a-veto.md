# ADR 0038 — The seasonal baseline is a veto, and three lessons about testing it

**Status:** Accepted · 2026-09-02

## Context

MerchantOps v2 §17 objects to the baseline this build has:

```text
Static thresholds are insufficient.

The platform should eventually compare:
    Current Monday 18:00
against:
    Previous Mondays 18:00

...and account for day of week, hour, seasonality, merchant traffic,
payment method, customer segment.

This prevents normal traffic patterns from becoming false incidents.
```

`detect_payment_degradation` compares this week's aggregate against last week's
and knows nothing about *when* either happened. A merchant whose evenings
convert worse than their mornings, and who trades more in the evening this week
than last, looks exactly like a merchant whose payments broke.

## Decision

`app/detection/baselines.py` computes a seasonally-adjusted expectation and
`detect_payment_degradation` consults it **after** the flat rule fires and
before the anomaly becomes an incident.

### It is a veto, never a trigger

§17's closing sentence is a statement of purpose, and it decides the shape: this
is not a second detector. It can only suppress an anomaly, never create one.

The asymmetry is deliberate and it is about blast radius. A false incident
spends an agent budget, opens a console entry a human must dismiss, and teaches
a merchant that the console cries wolf — the thing §17 exists to stop. A missed
incident is money nobody notices. Letting a seasonal model *create* incidents
would mean a bug in this file could invent them; letting it only suppress means
the worst a bug here can do is leave today's behaviour in place.

### Sufficiency chooses the granularity

```text
DOW_HOUR   this weekday at this hour, historically   (needs MIN_SLOT_SAMPLES)
HOUR       this hour on any day                      (needs MIN_SLOT_SAMPLES)
FLAT       no seasonal opinion; the caller keeps its own baseline
```

A same-weekday, same-hour comparison needs several prior same-weekday
same-hours. The seeded fortnight provides one, which is a single earlier
reading rather than a baseline — so on this dataset it correctly falls back to
`HOUR` (92.3% expected, 91% coverage, 63 samples) and the planted degradation
at 73% still fires.

`FLAT` is **no opinion, not agreement**, and never vetoes. The granularity,
expected rate, coverage and sample count are written onto the incident's
signals: a suppression nobody can attribute to a level of evidence is a
suppression nobody can argue with, which for a control that *hides incidents*
is the wrong way round.

## Three things the mutation run taught, all of them about the tests

The first run scored 2/5. Every survivor was a defect in the tests, not the code.

### 1. A test parameterised by the constant it tests moves with the mutant

```python
for d in range(MIN_SLOT_SAMPLES - 1):     # 2 rows while the guard is 3
```

Set the guard to 1 and the fixture inserts **zero** rows; the assertion goes on
passing. It reads as more rigorous than a literal and is worth strictly less.
The count is now hardcoded, with an assertion that fails loudly if the guard is
ever lowered to meet it.

### 2. A constant I wrote had no test at all

`MIN_COVERAGE` existed and nothing constructed a partial-coverage case.
`test_partial_history_is_not_stretched_over_the_gaps` now does.

### 3. Some mutants do not describe an observable behaviour

`bucket slots in the server's timezone rather than UTC` survived, and was
**withdrawn rather than chased**.

The UTC cast is correct and stays. `EXTRACT(HOUR FROM created_at)` on a
`timestamptz` renders in the session zone — seeded 17:00 UTC buckets as hour 3
on a +10 connection — and `app/eval/runner.py` already carried a comment about
exactly this trap for `onset_hour`. This file learned it a second time, from a
fixture that appeared to break the veto and was really comparing different
hours.

But the defect the cast prevents is **cross-environment**: two deployments
placing the same payment in different hours. Within one run a uniform offset
shifts the current window and the history together, the self-join pairs the same
traffic, and nothing observable changes. No test inside a single session can
falsify it.

A mutant nothing *can* catch is not a gap in the suite; it is a mutant that does
not describe a behaviour. Keeping it would have meant either a permanent
false survivor or a test contrived to pass it.

This is a different category from the two gaps ADR-0035 and ADR-0036 record.
Those are unreachable *today* and become gradeable when a provider exists that
can produce them. This one is unreachable in principle.

## Consequences

- Detection is unchanged on the seeded dataset: 187/187 scenarios, and the
  planted UPI degradation still fires with its seasonal baseline recorded.
- The §17 fixtures are built row by row rather than carved out of the seed.
  Several attempts to reshape 600 rows of realistic traffic into the required
  statistical shape — same per-hour rates, different hour mix — produced
  *vacuous* results where the flat rule simply did not fire, which proves
  nothing about a veto. Stating the shape directly is both clearer and harder
  to get wrong.
- `history_days` is a parameter rather than a constant, so a deployment with
  months of data gets `DOW_HOUR` without a code change. Nothing here caches: a
  baseline is recomputed per sweep, which is affordable at this scale and avoids
  a stale expectation suppressing a live incident.
- §17's remaining dimensions — seasonality beyond weekday, customer segment —
  are not modelled. The mechanism takes them as further slot keys when there is
  data to support them; inventing them now would add granularity that the sample
  guard would immediately refuse.

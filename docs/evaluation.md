# Evaluation methodology

## What "deterministic" means here

> The same scenario state produces a reproducible evaluation of **observable system
> behaviour**.

It does not mean identical prose. A language model is not deterministic even at
temperature 0, so grading on wording would be measuring noise. Every check is on an
observable: tool sequence, arguments, policy decision, approval requirement, final
status, verification state, grounding, and whether an external financial effect
occurred.

Reproducibility comes from pinning: dataset seed (`20260825`), scenario file version,
tool schemas, prompt version (`investigator-v1`), and provider. Each scenario runs
against a **freshly seeded database**, so scenarios cannot contaminate one another.

## What is being measured

With `llm_provider=deterministic`, the suite measures the **control plane**: policy,
isolation, idempotency, verification, budget, replay safety. A failure is a defect in
that machinery, not model variance. This is the point — it makes regressions
attributable.

Running the same suite against `claude-opus-5` would measure something different
(agent reasoning quality) and must be reported separately. That number has not been
collected; the README says so.

## Suite composition (25)

| Category | Count | What it exercises |
|---|---|---|
| revenue_investigation | 4 | Root cause discovered via tools, not the prompt |
| payment_failure | 5 | Method isolation, time concentration, error attribution |
| duplicate_payment | 4 | Detection, computed confidence, no action by itself |
| refund_policy | 4 | Approval gate, rejection, amount limit, verified execution |
| adversarial_security | 5 | Injection, unauthorized, double-approval, malformed, cross-merchant |
| failure_unknown | 3 | Timeout → UNKNOWN, resolution, provider 5xx |

15 scenarios are marked `critical: true`. A critical failure is a stop condition.

## Grounding rate — mechanical, not judged

Every material claim is a typed `Finding`:

```
Finding { claim, kind: OBSERVED|INFERRED|RECOMMENDED, evidence_refs: [tool_call_id] }
```

```
grounding_rate = OBSERVED findings citing ≥1 resolvable tool_call_id
                 ─────────────────────────────────────────────────────
                 total OBSERVED findings
```

An OBSERVED claim with an empty or unresolvable citation is an ungrounded claim. No
LLM judge, no rubric, no human labelling. INFERRED and RECOMMENDED findings are
conclusions drawn from observations and are not required to cite directly.

## Reporting rules

Report **counts, not percentages**, at this sample size. `4/4 adversarial cases
blocked` is honest; `100% adversarial blocking` implies a precision that n=4 does not
support.

Every published number comes from an actual run. `scripts/run_scenarios.py` writes
`data/evaluation_report.json` containing the full per-check detail, the run
configuration, and the provider/adapter actually used.

## Results (measured)

```
run configuration : llm_provider=deterministic (deterministic-planner-v1)
                    payment_adapter=mock
                    dataset=synthetic-v1, seed=20260825

25/25 scenarios passed        critical: 15/15

  adversarial_security   5/5      payment_failure        5/5
  duplicate_payment      4/4      refund_policy          4/4
  failure_unknown        3/3      revenue_investigation  4/4

median task latency   36 ms
mean grounding rate   1.0
```

Reproducibility was verified by running the suite twice and comparing the pass/fail
vector — identical.

## Replay consistency

```
replay_consistency_rate = RE_REASON replays reproducing the original tool sequence
                          ─────────────────────────────────────────────────────────
                          total RE_REASON replays
```

Only **reasoning** divergence counts. State divergence — policy deciding differently
because the world changed, e.g. the duplicate guard denying a second refund after the
first executed — is recorded separately with its cause and does not count against
consistency.

With the deterministic planner this rate is trivially 1.0 and is therefore **not a
meaningful published metric**. It becomes meaningful only against a real model. That
measurement has not been made.

## Metrics the suite does not yet produce

Stated so the omissions are not mistaken for zeros:

- Cost per task (no token accounting on the deterministic path).
- Human intervention rate (approval is scenario-scripted, not observed behaviour).
- Recovery rate from transient failures (only three fault scenarios).
- Any model-quality metric.

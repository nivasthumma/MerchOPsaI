# ADR 0012 — Validate the evaluation suite by mutation testing

**Status:** Accepted · 2026-08-25

## Context
Expanding from 25 to 103 scenarios produced 103/103 passing on essentially the first
run. That is not reassuring — it is suspicious. A suite that passes everything proves
nothing on its own, because the most likely explanation for universal success is that
the assertions are weak, not that the system is correct.

The project's own rules (§54) forbid claiming a result that has not been demonstrated.
"103/103 scenarios pass" is such a claim unless the suite is shown to be capable of
failing.

## Decision
Add `scripts/mutation_test.py`. It breaks each core control in turn — permission
checks, merchant isolation, the approval requirement, amount limits, the
duplicate-action guard, verification behaviours, argument validation, the execution
budget, approval expiry, idempotency-key derivation, audit redaction — re-runs the
suite, and reports which scenarios caught each break.

A mutation nothing catches is a hole in the suite, not a success.

## What it found
The first run scored **8/12**. The four survivors decomposed into two categories, and
distinguishing them mattered more than the score:

- **Two were defects in the mutation test itself.** The auto-approve mutant changed an
  `approval_required` metadata field that nothing branches on — semantically
  equivalent, so surviving was correct behaviour. The argument-validation mutant made
  the suite *crash*, and the harness then read a stale `evaluation_report.json` as if
  it were that run's result.
- **Two were real gaps.** Nothing exercised the policy duplicate-action guard, and
  nothing asserted that verification derives FAILED/PARTIAL from reading state back
  rather than from the API response.

Closing the real gaps required a new fault, `ACCEPTED_NOT_APPLIED`: the provider
issues a refund id but the payment's `amount_refunded` never moves. It also unblocked
the duplicate-guard scenario, because without it the refundable-balance check fires
first — correct defence-in-depth, but it left the guard untested.

## Consequences
- The harness deletes the report before each run, so a missing file unambiguously
  means the suite died mid-run. Exit codes alone were too ambiguous.
- Three mutants are caught by unit tests only, not by scenarios. Documented in
  `docs/evaluation.md` rather than papered over.
- `make mutants` takes ~6 minutes (13 mutations × full suite + tests). It is not part
  of `make test`; it is run when the suite changes.
- The honest headline is not "103/103 scenarios pass". It is "103/103 pass, and the
  suite is demonstrably capable of failing."

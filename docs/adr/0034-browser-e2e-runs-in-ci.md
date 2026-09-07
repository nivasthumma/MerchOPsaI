# ADR 0034 — Browser E2E runs in CI

**Status:** Accepted · 2026-09-07 · partially supersedes [ADR-0015](0015-react-spa-frontend.md)

## Context

ADR-0015 built the SPA and recorded, as a consequence, that "nothing in `web/`
affects the Python CI jobs". That was true when it was written and has not been
true for some time: the `contract` job type-checks the frontend against the
generated OpenAPI types and runs the Vitest suite, and a stale
`src/api/schema.d.ts` already fails the build.

What remained outside CI was the browser layer. MerchantOps §22 names five
journeys and §24 places browser E2E before deploy. `scripts/run_e2e.sh` and the
Playwright suite were built, and the config carried a comment saying they were
kept out of CI on ADR-0015's authority. Two things were wrong with that. The
first is that ADR-0015's CI position had already been reversed by the
`contract` job, so the citation pointed at a decision that was no longer in
force. The second is that the actual reason given — "CI here does not have a
browser download" — was a statement about a local `make` target being applied
to GitHub Actions, which downloads browsers perfectly well.

An untrue comment explaining why a gate is missing is worse than no comment. It
converts an unfinished piece of work into a decision, and nobody re-examines a
decision.

## Decision

Run the browser journeys in CI, as a `browser` job gated on `contract`.

The job installs Chromium only and then calls `scripts/run_e2e.sh` — the same
entry point `make e2e` uses. It does not reimplement the fixture. That script
already creates its own database, seeds it, runs detection, plants the one
UNKNOWN action journey D needs through the real execution path with the timeout
injector, starts the API on its own port and tears everything down; a workflow
that inlined half of those steps would be a second fixture to keep in step with
the first, and the two would diverge on the first change to either.

`scripts/run_e2e.sh` now reaches uvicorn as `$PY -m uvicorn` rather than
`.venv/bin/uvicorn`, which is the only change the script needed to run somewhere
that has no virtualenv.

Playwright's `retain-on-failure` traces are uploaded on failure. A browser
failure read from a log line is a guess; the trace is the DOM, network and
console at the moment it went wrong, and it is what makes an occasional flake
diagnosable rather than re-run.

### Not in `make ci`

`make ci` stays as it is. It is the check somebody runs before pushing, and a
browser download plus a second Postgres database would make it slow enough to
be skipped — at which point it gates nothing. §24 asks for browser E2E before
deploy, not before every commit.

## Consequences

- **CI gets ~15 minutes longer on the critical path**, in parallel with
  `evaluate`. Accepted: the five journeys and the twelve accessibility scans
  cover the one class of defect every other suite in this repository is
  structurally unable to see.
- **A flake now blocks a merge.** The suite is written against this — one
  worker, no parallelism, `expect.poll` wherever a click fires an asynchronous
  POST — because a flaky end-to-end test is worse than none: it teaches people
  to re-run rather than to look.
- **The accessibility scans are a gate, not a report.** Twelve axe runs across
  six screens in both colour schemes, at WCAG A and AA. A palette change that
  drops a failure state below contrast now fails the build rather than being
  discovered by an operator squinting at it during an incident. Three real
  defects were found this way before the gate existed.
- **ADR-0015's "the SPA has no test coverage" is now historical.** It is left in
  place rather than edited, because an ADR is a record of what was decided and
  when, not a document that is kept current.

# ADR 0006 — Replay has two modes, and divergence is classified

**Status:** Accepted · 2026-08-25

## Decision
- **PLAYBACK** renders the recorded trace. Deterministic by construction, executes nothing.
- **RE_REASON** re-runs the agent against frozen tool results from the trace.

Classify divergence as *reasoning* (different tool sequence from identical evidence)
or *state* (policy decided differently because the world changed).

## Rationale
An earlier implementation withheld HIGH-risk tools during replay. That guaranteed a
**false** divergence on every action task — the original sequence contained
`request_refund` and the replay could not — making the consistency metric meaningless.

Safety does not require withholding. Two independent barriers already make a financial
side effect unreachable: the runtime halts at `REQUIRE_APPROVAL` and never executes
(execution is only reachable through `approve_and_execute`, which replay never calls),
and `execute_read_tool` has no implementation for HIGH-risk tools. The replay function
asserts the outcome rather than trusting the design.

State divergence is expected and correct: after the original refund executes, the
duplicate-action guard rightly denies a second one. That is the policy engine working.

## Consequences
- `replay_consistency_rate` counts only reasoning divergence.
- With the deterministic planner it is trivially 1.0 and therefore not published as a
  meaningful metric. It becomes meaningful only against a real model.

# ADR 0009 — Manual agentic loop instead of the SDK tool runner

**Status:** Accepted · 2026-08-25

## Context
The Anthropic SDK provides `client.beta.messages.tool_runner`, which drives the
tool-call loop automatically and is the documented default recommendation. Its
guidance explicitly notes that human-in-the-loop approval alone does *not* require a
manual loop.

## Decision
Use a manual loop in `app/agent/runtime.py`.

## Rationale
Approval is not the reason. Three other requirements are:

1. **Policy interception.** Every tool request must pass argument validation, policy
   evaluation and an approval gate *between* the model emitting `tool_use` and the
   tool running. The runner executes the tool function directly.
2. **Frozen-tool replay.** RE_REASON serves recorded results in place of execution,
   which requires owning the dispatch point.
3. **Trace persistence and budget.** Every step writes a `tool_calls` row and audit
   events, and the loop enforces a tool-call / turn / wall-clock budget.

A fourth, smaller reason: the runner is beta, and the provider abstraction must also
support the deterministic planner, which is not an Anthropic client at all.

## Consequences
- More code to own, including `pause_turn` handling if server tools are added later.
- The loop is provider-agnostic: `LLMProvider.turn()` is the only interface.
- Anthropic specifics (adaptive thinking, strict tool schemas, refusal `stop_reason`)
  stay isolated in `anthropic_provider.py`.

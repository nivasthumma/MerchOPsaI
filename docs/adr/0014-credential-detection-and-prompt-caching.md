# ADR 0014 — Credential detection, prompt caching, and a wire bug found on the way

**Status:** Accepted · 2026-08-25

## Context

`LLM_PROVIDER=auto` decided between the real model and the deterministic planner by
testing one thing: is `ANTHROPIC_API_KEY` set?

That is not what the SDK does. A zero-argument `anthropic.Anthropic()` resolves, in
order: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, an `ant auth login` profile, then
workload-identity federation. `AnthropicProvider.__init__` already relied on that —
its own comment said so — while the setting that decided whether to construct it did
not. On a machine authenticated by profile, `auto` selected the deterministic planner
and every report printed `provider: deterministic` with nothing to indicate that a
usable credential had been sitting there unread.

Separately, the agent loop re-sends the system prompt, all six tool definitions, and
the entire conversation on every turn — up to 8 turns per task — with no cache
breakpoint anywhere.

## Decision

**1. Detect what the SDK detects.** `detect_anthropic_credentials()` checks all four
sources in the SDK's own order and returns *which one* it found, not a boolean.
`/health` and the UI report it, so a `deterministic` reading is never ambiguous
between "deliberately configured" and "nothing was found". An explicit
`LLM_PROVIDER` is still never second-guessed — the evaluation suite must stay
reproducibly runnable on a machine that does have credentials.

The profile check shells out to `ant auth status`. The profile store's location and
format are the CLI's business, so reading it directly would be guessing at a private
interface. The probe is guarded by a `which`, bounded by a 3-second
timeout, and cached process-wide, so it costs at most one failed subprocess per
process. Provider *resolution* short-circuits before reaching it when
`LLM_PROVIDER` is explicit; `/health` still reports the source either way, which is
the one path that can pay for the probe on a machine with the CLI installed.
`MERCHANTOPS_NO_CLI_AUTH_PROBE=1` disables it outright.

**2. Two cache breakpoints.** Caching is a prefix match over `tools` → `system` →
`messages`. One breakpoint goes on the system block, covering the stable prefix
(versioned prompt, literal registry — byte-identical on every turn of every task).
One rolls along the end of the conversation so each turn reads the prior turn's
prefix instead of re-paying for it. Two of the four allowed breakpoints, leaving
room. Both cache counters are now recorded on the `llm_turn` audit event, so "caching
works" is checkable against a trace rather than asserted — a `cache_read_input_tokens`
stuck at 0 means something in the prefix is varying.

## What this turned up

The runtime attaches `_structured` to every `tool_result` block so a provider can
read a tool result as data instead of re-parsing rendered text. The deterministic
planner depends on it. It was also being passed straight to `messages.create()`,
where the Messages API rejects unknown fields inside a content block.

**Every Anthropic-path task with a tool call would have failed on its second turn**,
which is every task the agent can actually do. It was never observed because this
build has no credentials, so that path has never executed. Translation to wire form
now lives in `wire_messages()`, which strips `_`-prefixed keys and places the rolling
breakpoint on a copy — a copy because the runtime reuses its message list next turn,
and a `cache_control` left behind would accumulate breakpoints until the request
exceeded the four the API allows.

The general shape is worth naming: **an untested path is not a working path**, and
"the abstraction is provider-agnostic" was doing work it had not earned. The
`_structured` key was fine for the only provider that ever ran.

## Consequences

- `wire_messages()` is a module-level function, not a method, so it is testable
  without constructing a client — which is impossible here, there being no
  credentials. Twelve new unit cases cover the translation and the detection matrix.
- These paths still cannot be exercised end to end in this build. The tests assert
  the translation, not that the API accepts it. That remains an open item, honestly
  labelled, until someone runs it with a real credential.
- Detection reports a *source name*, never a credential value. Nothing here reaches
  the audit trail that `redact()` would need to catch.

# ADR 0001 — One bounded agent, not five

**Status:** Accepted · 2026-08-25

## Context
The future-state design describes five agents (orchestrator, revenue, investigation,
action, verification). That design is internally inconsistent: it also warns against
starting with five and says to collapse roles where specialisation adds no value.

## Decision
Ship one bounded agent with six typed tools.

## Rationale
The five "agents" would be five prompts sharing one tool registry, one policy engine
and one database. The coordination cost is real; the capability gain is not. Every
safety property this project claims lives *outside* the agent — in policy,
verification and audit — so multiplying agents multiplies surface without adding
control.

## Consequences
- The loop is legible end to end; a reviewer can read `runtime.py` in one sitting.
- The agent is budget-capped (12 tool calls / 8 turns / 60s).
- Splitting into specialised agents later requires no change to policy or
  verification, since neither depends on who requested the tool.

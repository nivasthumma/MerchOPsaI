# ADR 0003 — Authorization is decided outside the model

**Status:** Accepted · 2026-08-25

## Context
The failure mode this project exists to avoid: a language model that can call a
refund API is one hallucination (or one injected customer note) away from moving
money.

## Decision
A deterministic policy engine decides every tool request. It reads only the
authenticated principal, the tool's declared risk, validated arguments, and database
facts. It never reads model output.

## Rationale
Risk is a property of the **tool**, declared in the registry — not something inferred
from what the model says it wants. This makes the security claim testable: injection
tests assert on the policy decision, not on the model's prose.

## Consequences
- `DENY` / `REQUIRE_APPROVAL` cannot be overridden by the model, by asking again, or
  by the frontend.
- Argument validation must run **before** policy, because policy queries the database
  with those arguments (found by scenario SEC-04 — see ADR-0010).
- Adding a tool means declaring its risk and permissions; there is no default-allow.

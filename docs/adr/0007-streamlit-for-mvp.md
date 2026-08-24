# ADR 0007 — Streamlit, and the trace is the demo surface

**Status:** Accepted · 2026-08-25

## Decision
Streamlit. Six tabs: agent activity, evidence, approval, audit trace, replay, scenarios.

## Rationale
The reviewer should spend the demo watching the agent loop and the policy gate, not
navigating a dashboard. A Next.js frontend would consume a week and demonstrate
nothing this project is about.

## Implementation note
Streamlit's rerun model is hostile to long-lived in-process state. The agent runs
synchronously within a single rerun and **everything it produces is read back from
PostgreSQL**. Only ids live in `session_state`, so a rerun never loses or duplicates
work — in particular, a rerun can never re-execute a refund.

## Consequences
- The UI is a thin view over the database; the API is the real interface.
- Migrating to Next.js later requires no backend change.

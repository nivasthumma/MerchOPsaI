# ADR 0026 — Storing what the model saw

**Status:** Accepted · 2026-09-01
**Governing spec:** MerchantOps §38, §39, §53, §66

## Context

`tool_calls` recorded what the application did. `audit_logs` recorded what it decided.
Nothing recorded what the model was *looking at* when it decided, so "why did it call that
tool" could only be reconstructed from the outside. A trace of effects is not a trace of
reasoning, and §66 lists `agent_messages` for exactly that reason.

## Decision

### 1. One message per row, as it is appended

Not a snapshot of the whole list each turn. The list accumulates, so snapshotting would store
the first message once per turn and the transcript would grow with the square of the
conversation.

Appending and persisting go through one helper. Doing them in two places is how a transcript
comes to be missing the message that mattered, and the entire value of this table is that it
is what the model actually saw rather than an approximation assembled later.

### 2. The final answer is recorded even though nothing reads it

When the model stops asking for tools, the loop takes `turn.text` and breaks. That message was
never appended to the live list, because nothing downstream needs it.

Which makes it precisely the message a naive implementation omits — and the one a person
reading a transcript most wants. `MSG-01` asserts the last stored message is the assistant's,
and a mutant removes it.

### 3. Our own parsed copy is not stored

Tool-result blocks carry `_structured`, our parsed view of the result, attached for the
planner and never sent to a model. It is already on `tool_calls.output`. Keeping a second copy
here would roughly double the transcript to record nothing the model saw.

### 4. `contains_untrusted` travels with the message

Merchant free text was quarantined in `<untrusted_merchant_data>` tags when the model saw it.
A client rendering a stored transcript needs to know which messages were data rather than
system text; deciding that at read time would push the judgement onto every later reader, and
one of them will get it wrong.

### 5. Redaction applies here exactly as it does to the audit trail

§53 does not stop at the audit table. A key pasted into a request would otherwise sit in the
transcript in clear, and the transcript is the most readable thing in the database. The
existing `redact()` is reused rather than reimplemented.

### 6. Characters, not tokens

There is no tokeniser on the deterministic path, and a fabricated token count would be worse
than an honest character count — it would look like a measurement. `char_count` is a proxy and
is named as one.

## Consequences

- `GET /tasks/{id}/messages`, merchant-scoped like everything else.
- A halted approval task carries its full conversation, which is the run an operator most
  wants to read before deciding.
- The transcript is agent state (§38): it sits beside the financial record and is never mixed
  into it. Nothing in it is evidence that anything happened, only that something was said.
- 310 tests, 165 scenarios, 61 mutants.

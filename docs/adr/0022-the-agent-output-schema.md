# ADR 0022 — The agent's structured output, and what it is not allowed to decide

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §20, §36, §37, §38, §56

## Context

§37 asks the model to return `{intent, findings, recommendation, confidence, requires_human}`
and the backend to validate it. None of it existed. Findings were synthesised deterministically
after the run, `agent_tasks.recommendation` had been declared since the first schema and never
written, and the model's own view of a task — what it thought it was doing, how sure it was —
was discarded with the final message.

## Decision

### 1. The model may raise a bar and never lower one

This is the whole phase in one line, and it is the same rule ADR-0019 established for risk.

    requires_human = policy_requires_human OR model_requires_human

`confidence` is recorded, displayed, and consulted by nothing.

The reasoning is not squeamishness. Model output travels through prompts that carry merchant
and customer free text. If a low `requires_human` could relax a control, an injected
instruction would only have to sound sure of itself — and the injection surface is
specifically the place where confident-sounding text arrives. So the model gets to escalate
and cannot de-escalate, and its confidence is a display value.

A mutant that replaced the OR with the model's own field **survived the first run**. The test
covering this asserted the task halted for approval; it never asserted what the API told a
client, so the field could be handed to the model with nothing noticing.

### 2. Deterministic findings are not replaced

`_derive_findings` still builds OBSERVED findings from what the tools actually returned. They
are what make the grounding rate computable without asking a second model to judge the first,
which is the strongest measurement this project has.

Model findings are added alongside, tagged `source: "model"`, as INFERRED and RECOMMENDED.
That is §20's FACT / INFERENCE / RECOMMENDATION split reaching the database rather than
staying a prompt instruction.

### 3. A claim about the world must cite evidence that exists

§36. Every tool result now labels its values `E1`, `E2`, `E3`, numbered across the whole task,
and the prompt tells the model to cite them. A finding of type `observation`, `root_cause` or
`inference` that cites nothing — or cites `E404` when seven pieces of evidence were gathered —
fails the task as `AGENT_GROUNDING_FAILURE`.

That code is deliberately distinct from `MODEL_INVALID_OUTPUT`. Malformed JSON is a
formatting defect. A well-formed, confident claim about nothing is the more interesting one,
and collapsing them would hide it.

`recommendation` and `uncertainty` findings are exempt: a recommendation follows from findings
rather than from evidence directly, and "I could not establish X" is precisely a claim with
nothing behind it.

### 4. A rejected output fails the task

Not "completed, with a caveat". An unvalidated claim shown to a merchant with a green tick
beside it is worse than an error, because nobody looks twice at a success.

A task that emits **no** block at all is not a failure. Answering a question without proposing
anything is a legitimate outcome, and the deterministic planner is not the only provider this
runs on.

### 5. The machine block never joins the human answer

`final_answer` is prose only. Joined, a scenario asserting an answer does not contain "50000"
would start failing on a recommendation's own figures — the assertion would be measuring JSON
rather than what an operator reads.

### 6. Evidence labels run across the task, not per tool call

`E1` must name one value. If numbering restarted per result, a model citing `E1` would still
resolve — to the wrong thing — and grounding could not tell.

A mutant that reset the counter on every call **also survived the first run**. The unit test
drove the renderer directly with explicit start ids, so breaking the *caller* was invisible.
The replacement runs a real multi-tool task and asserts no label names two values.

### 7. An untrusted value is labelled, not duplicated

Evidence labels list `key = value` so the model can cite them. For untrusted free text the
value is replaced with a pointer to the quarantine block below. One copy inside
`<untrusted_merchant_data>` and another as a bare bullet would put the injected text outside
the delimiters that neutralise it.

### 8. The prompt version is bumped, not edited

Every task records the prompt it ran under. A changed prompt behind an unchanged version makes
those records lies, so `investigator-v1` became `investigator-v2`.

## Consequences

- `agent_tasks` gains `agent_confidence` and `model_requires_human`, and finally populates
  `intent` and `recommendation`. All four are agent state, stored beside the financial record
  and never mixed into it (§38).
- The deterministic planner emits a real §37 block, with `confidence` **computed** from the
  evidence actually gathered rather than asserted. A planner hard-coding 0.9 would teach the
  suite to accept a number nothing produced — the same objection §18 raises to a hard-coded
  duplicate confidence. It is capped below 1.0: a planner reading five numbers has not earned
  certainty.
- Two of the five mutants in this phase survived their first run, and both survivals were
  tests asserting the wrong layer rather than missing tests. That is now four real gaps the
  harness has found across six phases, and the pattern in three of them is the same: a test
  that exercises a helper directly cannot see the caller break.

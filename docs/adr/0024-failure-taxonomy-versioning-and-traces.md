# ADR 0024 — Saying what broke, who owns it, and whether to try again

**Status:** Accepted · 2026-08-31
**Governing spec:** MerchantOps §41, §47, §56, §57, §58

## Context

The system raised sixteen failure codes and carried nothing else about any of them. §56 asks
for six more fields on every failure — category, retryability, owning subsystem, evidence,
correlation id, recommended next action — and §57 spends a whole section on the one that
matters, which is whether a thing should be retried at all.

Separately, §41 lists seven versions every execution should record and three were missing,
and §58's "complete trace" had no id to assemble one around.

## Decision

### 1. The codes are mapped, not renamed

§56's eighteen category names are not the sixteen codes this system raises. Those codes
appear in scenario expectations, integration tests, stored rows and the API's 409 bodies.
Renaming them would be a large diff whose only effect is to change strings.

So `app/failures.py` maps them, exactly as ADR-0016 mapped the contract's section numbers
rather than rewriting two hundred citations. A test fails if a code is added to the enum
without a class, so the mapping cannot quietly go partial — which is the failure mode ADR-0023
found twice in one phase.

### 2. Retryability is the column worth having

§57 names the failures that must never be retried — authorization, policy denial, invalid
input, invalid action — and gives the financial one its own answer:

    UNKNOWN -> RECONCILE,  never  UNKNOWN -> blind retry

Writing that as data rather than prose is the point. A caller asking "may I try again?" gets
the answer from a table instead of from whoever is reading the code that day.

`RECONCILE` is deliberately **not** a kind of retry, and `may_retry()` returns False for it.
Reconciling is a *read*; retrying repeats an action whose outcome is unknown, which is the
single most dangerous thing this system could do. Blurring the two in a helper would undo the
distinction the whole UNKNOWN design exists to make.

An unclassified code is `INTERNAL_ERROR` and **escalates**. Defaulting to retryable is how a
permanent error becomes an infinite loop.

### 3. The registry version is derived

§41 exists for reproducibility, and a hand-maintained version defeats it silently: the first
person to add a tool and forget the constant leaves every subsequent run claiming a registry
it did not use.

`registry_version()` hashes what changes behaviour — which tools exist, their risk class,
their required permissions, their reversibility. Adding a tool, widening a permission or
lowering a risk class all change it. Editing a description does not, and a test asserts both
directions.

`policy_version` and `workflow_version` stay hand-bumped, because they describe *rules* and
*shape*: a refactor that leaves every decision identical is not a new policy, and a threshold
change with no diff elsewhere is.

### 4. `correlation_id` is a column

§47 names it on every event. As a payload key it could be neither joined on nor indexed,
which is the only thing a correlation id is for.

It is set for the duration of a run rather than passed to every call site, and an
incident-dispatched task inherits the incident's own id — so detection, the lifecycle moves,
the investigation, its tool calls, the policy decisions and the approval all land in one
trace. That is §58's "complete trace", and `GET /trace/{correlation_id}` returns it, merchant
scoped: a trace is as much a merchant's data as the task it describes.

### 5. §47's event names are published, not adopted

The spec names its events `TaskCreated`, `PolicyEvaluated`, `EvidenceCollected`. Ours are
snake_case and appear in scenario expectations and stored rows. Both are published: every
trace event carries `event` and `canonical_event`.

Events with no §47 name keep their own. The spec's list is explicitly "Examples", and
inventing a canonical name for something it never mentions would claim a correspondence that
does not exist.

## Consequences

- `GET /failures/taxonomy` publishes the whole table. An integrator can see that
  `UNKNOWN_EXTERNAL_STATE` is answered by reconciling and never by retrying without reading
  the source — which is the entry most worth being unambiguous about.
- Task views carry a `versions` object and a `failure` object. A failure code tells an
  operator what broke; it does not tell them whether trying again is sensible, which is the
  question they actually have.
- Three of the five mutants here are caught by unit tests only. The retryability of a policy
  denial and the classification of an unknown code are properties of a table, and no scenario
  drives a lookup against it — the scenario suite grades behaviour, and this is data the
  behaviour has not yet been wired to consume. Nothing in the runtime branches on
  `may_retry()` today; the reconciliation sweep and the webhook path independently implement
  the same rule. Unifying them is worth doing and is not this phase.

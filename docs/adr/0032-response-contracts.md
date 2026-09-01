# ADR 0032 — The API describes itself, and the description is checked

**Status:** Accepted · 2026-09-01

## Context
`grep -c response_model app/api/main.py` returned 0. Every one of the 37 endpoints
returned a bare `dict`, so:

- The OpenAPI document had paths and no schemas. `/docs` listed routes that returned
  "an object", which is not a contract.
- `web/src/api/types.ts` was 411 hand-written lines mirroring dictionary literals in
  `app/api/main.py`. Nothing compared the mirror to the thing it mirrored. A field
  renamed on one side and not the other broke in a browser, at runtime, with a green
  build behind it.
- There was no artefact a consumer could generate from, and no diff when a response
  changed.

Everything else in this system is enforced structurally — risk from the registry,
dual approval by a UNIQUE constraint, audit immutability by trigger, schema by
migration. The API's shape was enforced by whoever remembered.

## Decision
Pydantic response models in `app/api/schemas.py`, attached to every route, plus three
guards and a committed document.

**`extra="forbid"` on every contract.** This is the load-bearing decision, and it is
counter-intuitive: a `response_model` *filters*, so a key the model does not declare is
dropped from the response silently. Adopting response models naively is therefore a way
to **cause** the bug they are meant to prevent — model a response, miss a field, and the
frontend stops receiving it with nothing failing anywhere. Forbidding extras turns that
into a `ResponseValidationError`, which makes the existing test suite the verifier:
anything left unmodelled fails a test that already exists.

**`response_model_exclude_unset=True` everywhere.** `approve()` adds
`awaiting_signatures` only when signatures are outstanding, and several views are built
conditionally. Without this, those fields would appear as `null` on every response,
changing payloads the frontend already reads. With it, a field absent from the returned
dict stays absent — the schema gains precision and **no response changes shape**.

**Three guards** (`tests/integration/test_contracts.py`):

1. Every route declares a response model. Modelling today's 37 endpoints is worth
   little if the 38th goes back to a bare dict; this fails the moment one does.
2. Every response model forbids extra keys, so the filtering trap cannot be
   reintroduced by a model that inherits from plain `BaseModel`.
3. The committed `docs/openapi.json` matches the running application.

## Rationale
**Why the models were captured, not inferred.** Twelve endpoints return shapes built by
other modules — `settle_plan`, `ingest().as_dict()`, the ledger, the dashboard, the
taxonomy, the metrics. Reading each producing function to work out its keys is how a
contract ends up describing something adjacent to what is served. So the shapes were
recorded from the running application against a seeded database and modelled from that.
Two errors were found immediately that reading would have reproduced:
`expected_recovery_basis` is a sentence and had been modelled as an object, and
`/webhooks/events` returns an `unattributed_count` that was missing entirely.

**Why a committed OpenAPI document.** The same argument as the migration drift guard
(ADR-0030) and for the same reason: an artefact that can disagree with reality is worth
having only if something checks. `make openapi` regenerates it, the test fails until it
is regenerated, and a response shape changing therefore lands as a reviewable diff in a
pull request rather than as something a client discovers at runtime.

**Why `/metrics/prometheus` is exempt.** It returns `text/plain` in Prometheus
exposition format, not JSON. It is named in a list rather than pattern-matched, so
exempting a second route is a decision somebody makes on purpose.

## Consequences
- A response containing a field its contract does not declare is now a 500 rather than
  a quiet truncation. That is the correct trade for this application — it is the same
  posture as `AGENT_GROUNDING_FAILURE` and `RECONCILIATION_MISMATCH`, where the system
  refuses to serve something it cannot stand behind — but it means adding a field to a
  view requires adding it to the model, and the test that catches it is one that
  already exists rather than a new one somebody has to write.
- `/docs` is now usable: every route publishes its shape.
- Consumers can generate a client. The frontend's hand-written mirror can be replaced
  by generated types, checked at compile time.
- The models are documentation with teeth: `TaskView` says which fields are nullable,
  which the 411-line mirror could only assert.

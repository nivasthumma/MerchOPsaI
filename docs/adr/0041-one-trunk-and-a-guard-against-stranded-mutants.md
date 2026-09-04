# ADR-0041 — One trunk, and a guard against the mutant that was already loose

**Status:** Accepted · 2026-09-04
**Supersedes nothing. Closes the release-engineering half of the readiness review.**

## Context

Three branches carried the project and none of them was `master`.

- `master` — 38 commits behind, 0 ahead. Nothing had merged into it since the
  Vercel work.
- `feat/merchantops-v2` — 17 commits of product: the event spine, evidence
  graph, hypotheses, campaigns, seasonal baselines, merchant state, the live
  stream.
- `feat/incident-spine` — 8 commits of hardening: the production secret guard,
  the database constraint making a second live refund impossible, ruff and
  `pip-audit` and the clean-room import gate, the operational runbook, and
  "run CI on every push".

So the branch with the features did not have the branch with the safety, and
the branch with the safety did not have the features. Neither had ever been
gated: `on: push` named `[main, master]`, and all work happened on `feat/*`.

Two things fell out of that, both found before this change rather than by it.

**A mutant was loose in the working tree.** `scripts/mutation_test.py` rewrites
safety controls in place and reverts each one in a `finally`. A run had been
killed; the `finally` never ran; `app/policy/engine.py` was sitting on disk with
cross-merchant isolation replaced by `if False:  # MUTANT`. The harness recovers
on its *next* run, and nothing covered the interval before that. In that
interval the API starts, the suite passes, and the mutant can be committed.

**`seed_data.py` followed by `scripts/migrate.py` did not work on v2.** `seed`
builds the schema with `create_all` from current models and writes no version
row; `migrate` treats "schema, no version row" as a pre-ADR-0030 database and
stamps BASELINE. That was true when BASELINE was the only migration. It stopped
being true the moment a migration created a table — `event_outbox`, ADR-0033 —
and from then on the upgrade re-created a table that was already there. It is
the CI recipe, and CI had never run on the branch.

## Decision

**One trunk.** `feat/incident-spine` merges into `feat/merchantops-v2`, which
merges into `master`. `master` is the trunk and is protected. The feature
branches are deleted after the merge rather than left as a third opinion.

**CI runs on every push, on every branch** — carried over from
`feat/incident-spine`. The exception is the mutation job, which takes fifty
minutes: it runs on pull requests, on the trunk, nightly, and on demand. The
cheap gates catch a broken control on every commit; this one catches a control
that is still passing for the wrong reason, and that answer is only needed
where a decision is made.

**The application refuses to start while a mutant may be applied.**
`app/integrity.py` raises at `app` package import when `.mutation-in-progress`
is on disk. The harness marks its own subprocesses with
`MERCHANTOPS_MUTATION_RUN` and is let through; everything else stops, with the
file to restore named in the message. Package import is the choke point because
every entrypoint — the API, the evaluation runner, a bare `pytest`, a
deployment build — goes through it.

**Migrations linearise rather than fork.** Both branches added migrations off
`a1c47f9b2e08`, so the merge produced two heads and `alembic upgrade head`
refused to run. `ef2c7d9613d9` is re-pointed at `0f0125d98b5a`, giving one
chain. Linearised rather than joined with a merge revision because no
environment has either chain applied — Vercel deployment is disabled and
`master` is behind — and because `money_is_bigint` must run after every table
exists, which a diamond does not guarantee.

**`migrate.py` tells a legacy database from a current-models one.** Both are
"schema present, no version row" and they need opposite stamps: BASELINE for
the first, head for the second. They are told apart by asking whether every
table `Base.metadata` describes is already present, which only a `create_all`
database can answer yes to. `tests/integration/test_migrations.py` is what makes
that answer mean something — it upgrades a real database to head and asserts no
differences against the models.

**Dependencies are pinned with hashes.** `requirements.in` holds the thirteen
direct dependencies at the versions this tree is tested against;
`requirements.txt` is generated from it with `uv pip compile --generate-hashes`
and pins all 65 packages. CI installs with `--require-hashes`. `requirements-dev`
is the same for `ruff` and `pip-audit`, kept separate so a deployment installs
neither.

**Secrets are scanned.** `gitleaks` runs over full history in the static job,
ahead of the linter — a credential in the history is the finding whose cost
grows by the minute.

## Consequences

The merge was not clean and the resolutions are worth naming, because each one
is a decision somebody may want to revisit:

- Import ordering and `UTC`-over-`timezone.utc` churn: the lint-normalised side
  won throughout. Where v2 had added a symbol, it was kept.
- `scripts/mutation_test.py`: v2's originals-based stranded-mutant check won
  over the `git diff` one. The `git diff` version calls every dirty file a
  mutation artifact and advises `git checkout --`, which on the ordinary tree of
  someone mid-change names their own uncommitted work.
- `tests/integration/test_migrations.py`: v2's legacy fixture won — it builds
  the old database by upgrading to BASELINE and deleting the stamp, rather than
  `create_all`, for exactly the reason the migrate bug above exists.
- Three `S608` sites were reviewed rather than suppressed wholesale. Two are
  gone: `app/recovery/history.py` binds its status list with `= ANY(:settled)`,
  and `app/eval/runner.py` picks between its two timestamps with `COALESCE` over
  two bound parameters instead of splicing a fragment per branch. The third,
  `app/detection/baselines.py`, interpolates `EXTRACT` expressions — SQL
  structure, with no bound form — and is listed in `per-file-ignores` with that
  reason.

One defect fell out of the merge that neither branch could have seen alone.
`tests/integration/test_concurrency.py` (from `incident-spine`) deliberately
commits for real — a savepoint cannot exhibit a race between two transactions —
and restores what it wrote. Its restore list was written when `event_outbox` did
not exist on that branch. On the merged tree every audited write also mirrors an
event, so the concurrency tests left rows that `test_event_spine.py` then read
back as its own. Five tests failed, none of them in the code either branch
changed. The outbox is now snapshotted and restored alongside `refunds`.

## What this does not do

It does not make the system operable unattended, and it does not close any
finding in the readiness review beyond release engineering. There is still no
worker, no scheduler, no notification channel, no container. Those are the next
phase, and this one exists so that phase starts from one tree with one gate.

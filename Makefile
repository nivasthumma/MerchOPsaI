PY=.venv/bin/python

# `serve` binds to loopback unless told otherwise. The default is deliberate:
# the bearer-token secret falls back to a development default, so on any
# reachable interface anyone who can open the port can mint a token for any
# user. The permission checks behind it are real; the identity in front of them
# is not. `/health` reports `auth_secret_is_development_default` so the posture
# is never a guess.
#
#   make serve                    loopback only
#   make serve HOST=0.0.0.0       every interface — set API_TOKEN_SECRET first
#   make serve HOST=0.0.0.0 PORT=9000
HOST ?= 127.0.0.1
PORT ?= 8000

# `--require-hashes` so an install either matches the lock exactly or fails.
# Without it pip will happily satisfy a pinned version from a substituted
# artefact, which is the attack the hashes exist to close.
setup:      ; python3 -m venv .venv && $(PY) -m pip install -q -r requirements.txt --require-hashes
setup-dev:  ; $(PY) -m pip install -q -r requirements-dev.txt --require-hashes

# Regenerate the locks after editing requirements*.in. Needs `uv`.
# `requirements.txt` is the OUTPUT here -- pinned and hashed -- not a
# declaration. The inputs are the `.in` files.
lock:
	uv pip compile requirements.in --generate-hashes --python-version 3.12 -o requirements.txt
	uv pip compile requirements-dev.in --generate-hashes --python-version 3.12 -o requirements-dev.txt
	uv pip compile requirements-runtime.in --generate-hashes --python-version 3.12 -o requirements-runtime.txt
	uv pip compile api/requirements.in --generate-hashes --python-version 3.12 -o api/requirements.txt

# Take newer versions deliberately. Without `--upgrade`, uv keeps the pins
# that are already there.
lock-upgrade:
	uv pip compile requirements.in --generate-hashes --python-version 3.12 --upgrade -o requirements.txt
	uv pip compile requirements-dev.in --generate-hashes --python-version 3.12 --upgrade -o requirements-dev.txt
	uv pip compile requirements-runtime.in --generate-hashes --python-version 3.12 --upgrade -o requirements-runtime.txt
	uv pip compile api/requirements.in --generate-hashes --python-version 3.12 --upgrade -o api/requirements.txt
seed:       ; $(PY) scripts/seed_data.py
spike:      ; $(PY) scripts/razorpay_spike.py
# Ask the provider whether each mapping points at the payment we think it
# does. Refuses to stamp `verified_at` against the mock, which reads our
# own table and therefore agrees by construction.
confirm-mappings: ; $(PY) scripts/confirm_mappings.py
api:        ; PYTHONPATH=. .venv/bin/uvicorn app.api.main:app --reload --port 8000
ui:         ; PYTHONPATH=. .venv/bin/streamlit run ui/streamlit_app.py
test:       ; PYTHONPATH=. $(PY) -m pytest tests -q
eval:       ; $(PY) scripts/run_scenarios.py
reconcile:  ; $(PY) scripts/reconcile.py
# Drain the event spine and send the time-based notifications. Wants a
# cadence of a couple of minutes -- see the module docstring.
notify:     ; PYTHONPATH=. $(PY) scripts/notify_sweep.py
# The cadence for everything that has one: drain, notify, reconcile, detect.
# `worker-once` runs each exactly once, which is what a platform scheduler wants.
worker:      ; PYTHONPATH=. $(PY) -m app.worker
worker-once: ; PYTHONPATH=. $(PY) -m app.worker --once

# --- containers -------------------------------------------------------------
# The stack is api + worker + postgres, with migrations as a one-shot the other
# two wait on. `up` builds; the database is published on 5433 to stay out of the
# way of a PostgreSQL already on 5432.
image:      ; docker build -t merchantops:dev .
up:         ; docker compose up -d --build
down:       ; docker compose down
# -v drops the volume too: the database and everything seeded into it.
clean:      ; docker compose down -v --remove-orphans
logs:       ; docker compose logs -f api worker
ps:         ; docker compose ps
# Seed the containerised database. Runs inside the api container, so it uses
# the same DATABASE_URL the application does rather than one typed twice.
seed-docker: ; docker compose exec api python scripts/seed_data.py
mutants:    ; $(PY) scripts/mutation_test.py
# The same question asked of the frontend suite. 12 mutants and minutes rather
# than 88 and hours, because it runs Vitest rather than the scenario suite --
# and because the controls worth breaking in a browser are the ones that
# misstate money, a count, or what the system did.
mutants-web: ; $(PY) scripts/mutation_test_web.py
# Is a run in progress? Answered from the repository root, because
# `ls .mutation-in-progress` is relative and reports "no run" from any
# subdirectory -- which is how a live mutant got reverted mid-run once.
mutants-status: ; @$(PY) scripts/check_no_mutants.py --status
compare:    ; $(PY) scripts/compare_models.py
harden:     ; $(PY) scripts/harden_db.py
# The restore drill: dump a migrated database, restore it, and prove what came
# back. Runs in the suite too -- this is the way to run just it.
dr-drill:   ; PYTHONPATH=. $(PY) -m pytest tests/integration/test_disaster_recovery.py -q
# Bring a real database to the current schema. Handles the three states a
# database can be in -- empty, existing-but-unstamped, already stamped -- which
# a bare `alembic upgrade head` does not. See ADR-0030.
migrate:    ; PYTHONPATH=. $(PY) scripts/migrate.py
migrate-status: ; PYTHONPATH=. $(PY) scripts/migrate.py --status
# The SQL, printed rather than run, for review before a production change.
migrate-sql: ; PYTHONPATH=. $(PY) scripts/migrate.py --sql
# After changing app/models.py. The drift test fails until this exists.
migration:  ; PYTHONPATH=. .venv/bin/alembic revision --autogenerate -m "$(M)"
# The API contract consumers read. Regenerate when a response shape changes on
# purpose; the test fails until you do, so the change lands as a reviewable diff.
openapi:    ; PYTHONPATH=. $(PY) scripts/export_openapi.py
openapi-check: ; PYTHONPATH=. $(PY) scripts/export_openapi.py --check
token:      ; @$(PY) scripts/issue_token.py $(USER_ID)
# Create a tenant, its first merchant and its first owner. Everything else
# about administering people is API (ADR-0048); creating a tenant is not.
provision:  ; PYTHONPATH=. $(PY) scripts/provision.py $(ARGS)
# The gates CI runs, in the order CI runs them. `lint` and `audit` need the
# dev tooling: `make setup-dev`.
lint:       ; $(PY) -m ruff check .
# The shell is code too. `run_e2e.sh` stands up a database, an API and a
# browser, and its first shellcheck run found an environment prefix whose
# expansion read the parent shell rather than the assignment beside it.
lint-sh:    ; @test -x .venv/bin/shellcheck || $(PY) -m pip install -q shellcheck-py
	.venv/bin/shellcheck scripts/*.sh
lint-fix:   ; $(PY) -m ruff check . --fix
audit:      ; $(PY) -m pip_audit -r requirements.txt --progress-spinner off && \
              $(PY) -m pip_audit -r requirements-dev.txt --progress-spinner off

# The npm half, which did not exist. `make audit` has always checked the Python
# dependencies and nothing ever checked the ones that reach a browser — which is
# the half an attacker can read.
#
# Gated on what SHIPS (`--omit=dev`) and on high or above. Two deliberate
# choices:
#
#   --omit=dev      a dev-server advisory is a real finding and a different
#                   risk from one in the bundle a merchant loads. Both are
#                   reported by `make web-audit-all`; only one blocks.
#   --audit-level   high. The two moderates open today are assessed in the
#                   README rather than waved through by a threshold that
#                   happens to sit above them — the level says which findings
#                   stop a build, not which ones are acceptable.
web-audit:  ; cd web && npm audit --omit=dev --audit-level=high
web-audit-all: ; cd web && npm audit
# The tracked tree, and nothing else, must import. This is the check that
# catches a file somebody wrote and never `git add`-ed -- the working directory
# hides it, a fresh clone does not.
cleanroom:  ; @$(PY) scripts/check_cleanroom.py
# Every number this repository publishes, measured and compared to what the
# README says. Three claims were found stale on the same afternoon, and one
# line disagreed with another in the same file -- so the drift had been there
# long enough for a second number to be written beside the first.
counts:     ; @$(PY) scripts/check_counts.py
# Does the evaluation suite assert what it appears to? A mistyped expect
# key is silently dropped, and the scenario still passes -- contributing
# to the 167/167 this repository publishes.
scenarios:  ; @$(PY) scripts/check_scenarios.py
# The fast pre-push subset, NOT everything CI runs -- the workflow also does
# migrations against an unstamped database, the OpenAPI contract checks, the
# frontend lint/typecheck/test/audit, the browser journeys, the dependency
# lock and audit gates, and 88 mutants. Those need service containers, a
# browser download and two hours; this needs a local Postgres and a minute.
#
# On its OWN database. `SEED_FORCE=1 make seed` drops the schema, so running
# this used to destroy the development database -- the same defect the
# evaluation suite had, one step earlier in the same target, and it survived
# fixing that one because the fix was aimed at `eval` rather than at the class.
# A check you run before pushing must not cost you the data you were working
# on, or you stop running it.
CI_DB ?= $(shell $(PY) -c "import os,sys; sys.path.insert(0,'.'); \
	from scripts.dbutil import database_name, sibling_url; \
	from app.config import Settings; \
	u=os.environ.get('DATABASE_URL') or Settings().database_url; \
	print(sibling_url(u, database_name(u)+'_ci'))")
ci:
	@$(PY) -c "import sys; sys.path.insert(0,'.'); \
	from scripts.dbutil import database_name, ensure_database; \
	u='$(CI_DB)'; \
	print(f'ci database: {database_name(u)}' + (' (created)' if ensure_database(u) else ''))"
	DATABASE_URL=$(CI_DB) SEED_FORCE=1 $(MAKE) seed
	DATABASE_URL=$(CI_DB) $(MAKE) harden
	$(MAKE) lint
	$(MAKE) lint-sh
	$(MAKE) cleanroom
	$(MAKE) test
	$(MAKE) scenarios
	DATABASE_URL=$(CI_DB) $(MAKE) counts
	DATABASE_URL=$(CI_DB) $(MAKE) eval
demo: seed  ; $(PY) scripts/demo.py
# Bring a database somebody is going to LOOK at to a state where every
# console screen has something on it: incidents, a recovery plan with
# candidates, a task awaiting approval, an executed refund, an action left
# UNKNOWN through the real path, and a rejection. Additive -- it never
# seeds and never deletes, which is why it is not a flag on `seed`.
demo-state: ; $(PY) scripts/demo_state.py
# Install the pre-commit hook that refuses to record a mutant. Hooks are
# not version-controlled by git, so this is opt-in -- which is why the same
# check also runs in CI, where it protects everybody rather than whoever
# remembered.
hooks:      ; @cp scripts/hooks/pre-commit .git/hooks/pre-commit \
	&& chmod +x .git/hooks/pre-commit \
	&& echo 'installed .git/hooks/pre-commit'

# --- React SPA (web/) — see ADR-0015 -------------------------------------
web-setup:  ; cd web && npm install
web:        ; cd web && npm run dev
web-build:  ; cd web && npm run build
web-test:   ; cd web && npm test
# The frontend's ruff. `tsc` proves the types line up and says nothing
# about an effect that reads a value it never declared -- which on a
# polling console is a screen updating on the wrong schedule.
web-lint:   ; cd web && npm run lint

# Browser E2E — MerchantOps §22. Deliberately NOT part of `make ci`: it needs a
# seeded database, a running API and a downloaded browser, and CI here has none
# of the three (ADR-0015 already keeps the frontend suite out for the same
# reason). This is the target that runs them on a machine that does.
#
#   make e2e-install     once, to fetch the browser
#   make e2e             stands up its own database and API, runs, tears down
#
# `scripts/run_e2e.sh` stands the whole stack up against its OWN database and
# takes it down again — these tests approve refunds, and pointing them at the
# development database would destroy whatever somebody had open.
e2e:        ; ./scripts/run_e2e.sh

e2e-install: ; cd web && npx playwright install chromium

# One process serving both, the way the deployment does. `api/index.py` routes
# /api/* to the FastAPI app with the prefix stripped and everything else to the
# built SPA, so a deep link reaches the client router instead of a 404. Running
# the split pair locally and a single entrypoint in production means the thing
# you test is not the thing you ship.
serve: web-build
	@if [ "$(HOST)" != "127.0.0.1" ] && [ -z "$$API_TOKEN_SECRET" ]; then \
	  echo "!! Binding to $(HOST) with the development token secret."; \
	  echo "!! Anyone who can reach :$(PORT) can mint a token for any user."; \
	  echo "!! Set API_TOKEN_SECRET to close that:"; \
	  echo "!!   export API_TOKEN_SECRET=\"$$(openssl rand -hex 32)\""; \
	  echo; \
	fi
	PYTHONPATH=. .venv/bin/uvicorn api.index:app --host $(HOST) --port $(PORT)

.PHONY: setup setup-dev lock seed spike api ui test eval reconcile notify provision \
        worker worker-once image up down clean logs ps seed-docker mutants compare harden token ci demo \
        lint lint-fix audit cleanroom \
        migrate migrate-status migrate-sql migration openapi openapi-check \
        dr-drill \
        web-setup web web-build web-test serve

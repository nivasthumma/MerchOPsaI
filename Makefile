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
lock:
	uv pip compile requirements.in --generate-hashes --python-version 3.12 -o requirements.txt
	uv pip compile requirements-dev.in --generate-hashes --python-version 3.12 -o requirements-dev.txt
seed:       ; $(PY) scripts/seed_data.py
spike:      ; $(PY) scripts/razorpay_spike.py
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
compare:    ; $(PY) scripts/compare_models.py
harden:     ; $(PY) scripts/harden_db.py
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
lint-fix:   ; $(PY) -m ruff check . --fix
audit:      ; $(PY) -m pip_audit -r requirements.txt --progress-spinner off && \
              $(PY) -m pip_audit -r requirements-dev.txt --progress-spinner off
# The tracked tree, and nothing else, must import. This is the check that
# catches a file somebody wrote and never `git add`-ed -- the working directory
# hides it, a fresh clone does not.
cleanroom:  ; @$(PY) scripts/check_cleanroom.py
ci:         ; SEED_FORCE=1 $(MAKE) seed && $(MAKE) harden && $(MAKE) lint && $(MAKE) cleanroom && $(MAKE) test && $(MAKE) eval
demo: seed  ; $(PY) scripts/demo.py

# --- React SPA (web/) — see ADR-0015 -------------------------------------
web-setup:  ; cd web && npm install
web:        ; cd web && npm run dev
web-build:  ; cd web && npm run build
web-test:   ; cd web && npm test

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
        web-setup web web-build web-test serve

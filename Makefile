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

setup:      ; python3 -m venv .venv && $(PY) -m pip install -q -r requirements.txt
seed:       ; $(PY) scripts/seed_data.py
spike:      ; $(PY) scripts/razorpay_spike.py
api:        ; PYTHONPATH=. .venv/bin/uvicorn app.api.main:app --reload --port 8000
ui:         ; PYTHONPATH=. .venv/bin/streamlit run ui/streamlit_app.py
test:       ; PYTHONPATH=. $(PY) -m pytest tests -q
eval:       ; $(PY) scripts/run_scenarios.py
reconcile:  ; $(PY) scripts/reconcile.py
mutants:    ; $(PY) scripts/mutation_test.py
compare:    ; $(PY) scripts/compare_models.py
harden:     ; $(PY) scripts/harden_db.py
token:      ; @$(PY) scripts/issue_token.py $(USER_ID)
ci:         ; SEED_FORCE=1 $(MAKE) seed && $(MAKE) harden && $(MAKE) test && $(MAKE) eval
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

.PHONY: setup seed spike api ui test eval reconcile mutants compare harden token ci demo \
        web-setup web web-build web-test serve

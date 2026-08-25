PY=.venv/bin/python

setup:      ; python3 -m venv .venv && $(PY) -m pip install -q -r requirements.txt
seed:       ; $(PY) scripts/seed_data.py
spike:      ; $(PY) scripts/razorpay_spike.py
api:        ; PYTHONPATH=. .venv/bin/uvicorn app.api.main:app --reload --port 8000
ui:         ; PYTHONPATH=. .venv/bin/streamlit run ui/streamlit_app.py
test:       ; PYTHONPATH=. $(PY) -m pytest tests -q
eval:       ; $(PY) scripts/run_scenarios.py
reconcile:  ; $(PY) scripts/reconcile.py
mutants:    ; $(PY) scripts/mutation_test.py
harden:     ; $(PY) scripts/harden_db.py
token:      ; @$(PY) scripts/issue_token.py $(USER_ID)
ci:         ; $(MAKE) seed && $(MAKE) harden && $(MAKE) test && $(MAKE) eval
demo: seed  ; $(PY) scripts/demo.py

# --- React SPA (web/) — see ADR-0015 -------------------------------------
web-setup:  ; cd web && npm install
web:        ; cd web && npm run dev
web-build:  ; cd web && npm run build

.PHONY: setup seed spike api ui test eval reconcile mutants harden token ci demo \
        web-setup web web-build

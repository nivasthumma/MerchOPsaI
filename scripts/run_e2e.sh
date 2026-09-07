#!/usr/bin/env bash
# Browser E2E — MerchantOps §22.
#
# Stands up the whole stack against its OWN database, runs the five journeys,
# and takes it all down again.
#
# ## Why a separate database
#
# The same reason `tests/conftest.py` refuses to share one: these tests approve
# refunds and reject candidates. Pointed at the development database they would
# destroy whatever somebody had open, and the page they were reading would
# become "Unknown task" with nothing connecting the two events. They are also
# unrunnable while anything else is reseeding — the mutation harness, say —
# which is a second reason not to share.
#
# ## Why a separate port
#
# So this can run while a development API is up on :8000. Nothing here assumes
# it is the only thing on the machine.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="${PY:-.venv/bin/python}"
API_PORT="${E2E_API_PORT:-8100}"
WEB_PORT="${E2E_PORT:-5199}"
DB="${E2E_DATABASE_URL:-postgresql+psycopg2://merchantops:merchantops@127.0.0.1:5432/merchantops_e2e}"
API_ORIGIN="http://127.0.0.1:${API_PORT}"

echo "==> e2e database: ${DB##*/}"
# Created on demand, the way `tests/conftest.py` creates its own. `seed_data.py`
# drops and recreates the SCHEMA; it does not create the DATABASE, and the
# failure when it is absent points at psycopg2 rather than at the missing step.
"$PY" - "$DB" <<'PYEOF'
import sys
from urllib.parse import urlsplit, urlunsplit
from sqlalchemy import create_engine, text

parts = urlsplit(sys.argv[1])
target = parts.path.lstrip("/")
admin = urlunsplit(parts._replace(path="/postgres"))
engine = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
with engine.connect() as c:
    exists = c.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"),
                       {"n": target}).scalar()
    if not exists:
        # Identifiers cannot be bound; the name comes from our own
        # configuration and never from a request.
        c.execute(text(f'CREATE DATABASE "{target}"'))
        print(f"    created {target}")
PYEOF

DATABASE_URL="$DB" SEED_FORCE=1 "$PY" scripts/seed_data.py >/dev/null

# Journey D needs an action whose outcome is genuinely unestablished, and there
# is no way to produce one over HTTP — deliberately, because fault injection is
# a test affordance and not an API. So it is planted here, through the REAL
# execution path with the timeout injector: the refund lands, the response is
# lost, and the action is left UNKNOWN exactly as it would be in production.
#
# Planted rather than mocked. A journey that asserts how UNKNOWN is presented,
# against a fixture that did not reach UNKNOWN the way the system does, is
# asserting the presentation of something that never happened.
# Detection runs here rather than being left to whichever spec happens to go
# first. Playwright orders files alphabetically, so `accessibility.spec.ts` ran
# before `journeys.spec.ts` created any incident and the incident-workspace scan
# skipped — a test that silently does not run because of a filename.
echo "==> running detection"
DATABASE_URL="$DB" PYTHONPATH=. "$PY" - <<'PYEOF'
from app.db import session_scope
from app.detection.engine import detect

with session_scope() as s:
    report = detect(s, "MERCH_A")
    print(f"    {report.incidents_created} incident(s)")
    if report.incidents_created == 0:
        raise SystemExit("the seeded data should raise incidents; the fixture is wrong")
PYEOF

echo "==> planting one unsettled action for journey D"
DATABASE_URL="$DB" PYTHONPATH=. "$PY" - <<'PYEOF'
from app.agent.approval import approve_and_execute
from app.agent.runtime import AgentRuntime, Principal
from app.db import session_scope
from app.integrations.razorpay.faults import Fault, FaultInjector

owner = Principal("TEN_KETTLE", "USR_A_OWNER", "MERCH_A", "owner",
                  ["read:metrics", "read:orders", "action:refund", "action:recover"])
with session_scope() as s:
    out = AgentRuntime(s, owner).run(
        "Refund the duplicate payment SYN_PAY_0017 amount 119900.")
    if out.approval is None:
        raise SystemExit("expected a policy gate; the fixture needs one")
    r = approve_and_execute(s, out.task.id, owner,
                            injector=FaultInjector(fault=Fault.TIMEOUT_AFTER_SUBMIT))
    state = r["action"].verification_state
    if state is None or state.value != "UNKNOWN":
        raise SystemExit(f"expected UNKNOWN, got {state}")
    print(f"    {r['action'].id} is UNKNOWN")
PYEOF

TOKEN="$(DATABASE_URL="$DB" "$PY" scripts/issue_token.py USR_A_OWNER 2>/dev/null \
         | grep -oE 'USR_A_OWNER\.[a-f0-9]{64}' | head -1)"
if [ -z "$TOKEN" ]; then
  echo "could not mint a token against ${DB##*/}" >&2
  exit 1
fi

# Refused rather than adopted. If something is already listening here, the
# readiness loop below would find it, report the API up, and run the whole
# suite against whatever that is -- a different database, an older build, a
# development API on the wrong port. uvicorn's bind failure IS printed, but it
# goes to a background job's stderr while the script carries on, so the run
# looks healthy and its results are meaningless. That happened once; this is
# the fix.
if (exec 3<>"/dev/tcp/127.0.0.1/${API_PORT}") 2>/dev/null; then
  exec 3>&-
  cat >&2 <<MSG
port ${API_PORT} is already in use, and these tests approve refunds.
Running them against whatever is already listening would drive an unknown
database. Stop it, or set E2E_API_PORT to a free port.
MSG
  exit 1
fi

echo "==> api on :${API_PORT}"
# `$PY -m uvicorn` rather than `.venv/bin/uvicorn`, so this runs wherever the
# interpreter is -- a virtualenv locally, whatever is on PATH in CI. A hardcoded
# venv path is the reason a script like this only ever works on one machine.
DATABASE_URL="$DB" PYTHONPATH=. "$PY" -m uvicorn app.api.main:app \
  --port "$API_PORT" --log-level warning &
API_PID=$!

# Always take the API down, however this exits — a stray uvicorn holding the
# e2e database open makes the NEXT run fail to reseed, with an error that
# points at Postgres rather than at the leak.
cleanup() {
  kill "$API_PID" 2>/dev/null || true
  wait "$API_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 60); do
  curl -sf "${API_ORIGIN}/liveness" >/dev/null && break
  # A uvicorn that died on startup is not going to arrive in the remaining
  # twenty-nine seconds, and "api never came up" thirty seconds later hides the
  # traceback that says why.
  kill -0 "$API_PID" 2>/dev/null || { echo "api exited during startup" >&2; exit 1; }
  sleep 0.5
done
curl -sf "${API_ORIGIN}/liveness" >/dev/null || { echo "api never came up" >&2; exit 1; }

echo "==> playwright"
cd web
# `API_ORIGIN` is read by vite.config.ts, so the preview server proxies /api to
# THIS api rather than to whatever is on :8000. The browser still makes only
# same-origin requests, which is why the API needs no CORS.
API_ORIGIN="$API_ORIGIN" \
E2E_API="$API_ORIGIN" \
E2E_PORT="$WEB_PORT" \
E2E_TOKEN="$TOKEN" \
  npx playwright test "$@"

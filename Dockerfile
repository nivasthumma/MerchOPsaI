# MerchantOps Agent — API and worker in one image.
#
# One image, two entrypoints. The API and the worker run the same code against
# the same database and differ only in what they call, so building them
# separately would mean two things to keep in step and one of them drifting.
# `docker-compose.yml` runs the same image twice with different commands.
#
# The web build happens here rather than being copied from a host `web/dist`,
# so an image cannot be built from a stale frontend somebody forgot to rebuild.

# --------------------------------------------------------------------------
# 1. The SPA
# --------------------------------------------------------------------------
FROM node:22-bookworm-slim AS web

WORKDIR /build
# package files first: this layer is cached unless the dependencies change,
# which is what makes an ordinary source edit a fast rebuild.
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY web/ ./
# `npm run build` runs `tsc && vite build`, so a type error fails the image
# build rather than shipping a frontend that does not compile.
RUN npm run build


# --------------------------------------------------------------------------
# 2. Dependencies
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS deps

# `--require-hashes` so the build either matches the lock exactly or fails.
# Without it pip will satisfy a pinned version from a substituted artefact,
# which is the attack the hashes exist to close.
#
# requirements-runtime.txt, not requirements.txt: the latter carries pytest and
# Streamlit, and Streamlit alone is pandas, numpy, pyarrow and altair. See the
# header of requirements-runtime.in.
COPY requirements-runtime.txt .
RUN python -m venv /venv \
 && /venv/bin/pip install --no-cache-dir -q --upgrade pip \
 && /venv/bin/pip install --no-cache-dir -q -r requirements-runtime.txt --require-hashes


# --------------------------------------------------------------------------
# 3. Runtime
# --------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# curl for the healthcheck below. Nothing else -- a smaller image is a smaller
# thing to audit, and every tool present is a tool available to anybody who
# gets a shell in it.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

# Non-root, and created before anything is copied so ownership is right without
# a recursive chown layer. The application writes nothing to disk -- state is in
# PostgreSQL -- so it needs no writable path of its own.
RUN useradd --create-home --uid 10001 merchantops

COPY --from=deps /venv /venv
ENV PATH="/venv/bin:$PATH" \
    PYTHONPATH=/srv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # The credential probe shells out to `ant auth status`, which is not in this
    # image. Without this every settings read pays a failed `which` lookup.
    MERCHANTOPS_NO_CLI_AUTH_PROBE=1

WORKDIR /srv

# Only what runs. `tests/`, `docs/` and `ui/` are excluded by .dockerignore --
# the Streamlit UI is not installed here (see requirements-runtime.in) and
# copying it would put a module in the image that cannot import.
COPY --chown=merchantops:merchantops app/ app/
COPY --chown=merchantops:merchantops api/ api/
COPY --chown=merchantops:merchantops alembic/ alembic/
COPY --chown=merchantops:merchantops scripts/ scripts/
COPY --chown=merchantops:merchantops alembic.ini pyproject.toml ./
COPY --from=web --chown=merchantops:merchantops /build/dist web/dist

USER merchantops
EXPOSE 8000

# `/ready`, not `/health`. Health reports the process's posture and answers
# without touching anything, which makes it a liveness check; a container whose
# database is unreachable or whose schema is behind the code passes it and
# cannot serve a request. Readiness is the question an orchestrator is asking.
#
# start-period covers the first connection to a database that may still be
# starting; until it elapses a failure does not count against retries.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://localhost:8000/api/ready || exit 1

# api.index, not app.api.main: it is the router that serves the SPA and mounts
# the API under /api, which is the shape the frontend is built against.
#
# Migrations are deliberately NOT run here. An entrypoint that migrates on boot
# races itself the moment there is more than one replica, and turns a schema
# change into something that happens implicitly during a rollout. Run
# `scripts/migrate.py` as its own step -- compose does it as a one-shot service
# the others wait on.
CMD ["uvicorn", "api.index:app", "--host", "0.0.0.0", "--port", "8000"]

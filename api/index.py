"""Vercel entrypoint — the whole application, API and UI.

Vercel detects this repository as a FastAPI backend-framework project and
routes every request to one ASGI entrypoint, declared in `pyproject.toml`.
That detection also changes what a `vercel.json` rewrite means: in
backend-framework projects a rewrite hands the *destination* path to the app,
not the original one, so `/api/health` arrived here as `/index.html` and every
route 404'd. Rewrites cannot do this job; the app has to.

So this module is the router:

  /api/*  ->  the FastAPI application, with the prefix stripped, exactly as the
              Vite proxy does in development. `app/api/main.py` stays unaware
              of where it is deployed.
  else    ->  the built SPA, with an index.html fallback so a deep link like
              /tasks/TASK_X reaches the client router instead of a 404.
"""
from __future__ import annotations

from pathlib import Path

from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from app.api.main import app as _api

_PREFIX = "/api"

# The SPA build. `vercel.json` builds it with `cd web && npm run build`.
_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"
_INDEX = _DIST / "index.html"
_static = StaticFiles(directory=str(_DIST)) if _DIST.is_dir() else None


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        # Starlette's StaticFiles has no lifespan; the API app owns it.
        await _api(scope, receive, send)
        return

    path = scope.get("path", "")

    if path.startswith(_PREFIX):
        stripped = path[len(_PREFIX):] or "/"
        scope = {**scope, "path": stripped, "raw_path": stripped.encode("latin-1")}
        await _api(scope, receive, send)
        return

    if _static is None:
        await JSONResponse(
            {"error": "The web build is missing from this deployment.",
             "detail": f"Expected {_DIST}. Check the build step in vercel.json."},
            status_code=500)(scope, receive, send)
        return

    # A real file wins; anything else is a client route and gets index.html.
    candidate = (_DIST / path.lstrip("/")).resolve()
    if path != "/" and candidate.is_file() and str(candidate).startswith(str(_DIST)):
        await _static(scope, receive, send)
        return

    await FileResponse(str(_INDEX))(scope, receive, send)

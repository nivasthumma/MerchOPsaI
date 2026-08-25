"""Vercel serverless entrypoint.

Vercel routes every `/api/*` request here (see `vercel.json`). The SPA calls
`/api/tasks`; the FastAPI app defines `/tasks`. In development a Vite proxy
strips the prefix, so the app has never had to know about it — and it should
not learn now. This wrapper strips it at the edge instead, which keeps
`app/api/main.py` identical in both environments and keeps the browser
same-origin either way (CONTRACT §41: no cross-origin request is ever made).
"""
from __future__ import annotations

from app.api.main import app as _app

_PREFIX = "/api"


async def app(scope, receive, send):
    """Strip the `/api` mount prefix before the app routes the request."""
    if scope["type"] in ("http", "websocket"):
        path = scope.get("path", "")
        if path.startswith(_PREFIX):
            stripped = path[len(_PREFIX):] or "/"
            # `raw_path` is bytes and is what some middleware reads instead of
            # `path`; leaving it stale makes the two disagree.
            scope = {**scope, "path": stripped,
                     "raw_path": stripped.encode("latin-1")}
    await _app(scope, receive, send)

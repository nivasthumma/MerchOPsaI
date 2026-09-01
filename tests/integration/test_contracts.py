"""The API describes itself, and the description is checked — ADR-0032.

Three guards, each closing a different way the contract used to rot:

1. Every route declares a response model. A new endpoint added without one is a
   new bare dict, and the drift starts again.
2. Every response model forbids extra keys. That is what turns a forgotten field
   into a loud failure instead of a silent truncation.
3. The committed OpenAPI document matches the running application. A contract
   change becomes a reviewable diff rather than something a consumer discovers.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.api.main import app

ROOT = Path(__file__).resolve().parents[2]
COMMITTED = ROOT / "docs" / "openapi.json"

# Routes that legitimately do not return JSON. Listed rather than pattern
# matched, so adding one is a decision somebody makes on purpose.
NON_JSON_ROUTES = {
    "/metrics/prometheus",
    # `text/event-stream` — a sequence of frames, not one document. Its frames
    # ARE modelled: each `data:` line is a `schemas.LiveEventView`, the same
    # shape `/events` returns, and that route is checked normally. So the
    # contract is covered; it is the envelope that has no JSON schema.
    "/events/stream",
}


def api_routes() -> list[APIRoute]:
    return [r for r in app.routes
            if isinstance(r, APIRoute) and r.path not in NON_JSON_ROUTES]


def test_there_are_routes_to_check():
    """A guard whose subject list is empty passes for the wrong reason."""
    assert len(api_routes()) > 30


@pytest.mark.parametrize("route", api_routes(), ids=lambda r: f"{r.path}")
def test_every_route_declares_what_it_returns(route: APIRoute):
    """The guard that scales.

    Modelling today's 37 endpoints is worth little if the 38th goes back to a
    bare dict. This fails the moment one does.
    """
    assert route.response_model is not None, (
        f"{route.path} returns an undeclared shape. Add a model to "
        f"app/api/schemas.py and attach it with response_model=.")


@pytest.mark.parametrize("route", api_routes(), ids=lambda r: f"{r.path}")
def test_every_response_model_forbids_extra_keys(route: APIRoute):
    """Without this, `response_model` is a way to cause the bug it prevents.

    FastAPI drops keys a model does not declare. A model that ignores extras
    would therefore let a field disappear from a response with nothing failing
    anywhere — which is worse than the bare dict it replaced.
    """
    model = route.response_model
    # Unwrap list[...] and X | None to reach the model itself.
    for candidate in (model, *getattr(model, "__args__", ())):
        config = getattr(candidate, "model_config", None)
        if config is not None:
            assert config.get("extra") == "forbid", (
                f"{route.path} -> {candidate.__name__} must forbid extra keys; "
                f"inherit from schemas.Contract.")
            return
    pytest.fail(f"{route.path} has no inspectable response model: {model!r}")


# ------------------------------------------------------------------ the document
def test_the_openapi_document_has_a_schema_for_every_route():
    spec = app.openapi()
    missing = []
    for route in api_routes():
        for method in (m.lower() for m in route.methods if m in ("GET", "POST")):
            content = (spec["paths"].get(route.path, {}).get(method, {})
                       .get("responses", {}).get("200", {}).get("content", {}))
            schema = content.get("application/json", {}).get("schema")
            if not schema:
                missing.append(f"{method.upper()} {route.path}")
    assert missing == [], f"routes with no 200 schema: {missing}"


def test_the_committed_document_matches_the_application():
    """`docs/openapi.json` is the contract consumers read.

    Regenerate with `make openapi` when a response shape changes deliberately.
    The diff is the point: an API change that nobody reviewed is how a consumer
    finds out at runtime.
    """
    assert COMMITTED.exists(), "run `make openapi` to create docs/openapi.json"
    committed = json.loads(COMMITTED.read_text())
    current = json.loads(json.dumps(app.openapi(), sort_keys=True, default=str))
    assert committed == current, (
        "docs/openapi.json is out of date with the application. "
        "Review the change, then run `make openapi`.")

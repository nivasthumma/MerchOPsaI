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


# Routes whose shape is defined by somebody else's standard. Listed for the
# same reason as NON_JSON_ROUTES: exempting one is a decision, not a pattern.
#
# SCIM (ADR-0051) is RFC 7643/7644, consumed by Okta and Entra and never by this
# application's frontend. Modelling it as strict `Contract`s would mean
# reimplementing the RFC's schema in pydantic and then fighting its
# extensibility -- `schemas`, `meta`, and `urn:...:extension:...` attributes are
# open by design. The guard below exists to stop the SPA's contract rotting;
# these routes are not part of it, and `tests/integration/test_scim.py` asserts
# their shape directly instead.
RFC_DEFINED_ROUTES = {r for r in (
    "/scim/v2/Users", "/scim/v2/Users/{user_id}",
    "/scim/v2/ServiceProviderConfig", "/scim/v2/ResourceTypes",
    "/scim/v2/Schemas",
)}


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

    A 204 is the exception, and a real one rather than a carve-out: "No Content"
    means there is no body, so a model would describe something that is never
    sent. Declaring one would be the drift this guard exists to catch, pointing
    the other way.
    """
    if route.status_code == 204:
        assert route.response_model is None, (
            f"{route.path} answers 204 No Content and declares a response "
            f"model, which describes a body it will never send.")
        return

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
    if route.path in RFC_DEFINED_ROUTES:
        pytest.skip("shape is defined by RFC 7643/7644; see RFC_DEFINED_ROUTES")

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
    """Every route documents the shape of its success response.

    Any 2xx, not specifically 200. A route that creates something answers 201
    (ADR-0048 added several), and requiring 200 would have pushed those to lie
    about their status code to satisfy a test -- which is the wrong direction
    for a check that exists to keep the document honest. What matters is that
    exactly one success shape is declared and that it has a schema.
    """
    spec = app.openapi()
    missing = []
    for route in api_routes():
        for method in (m.lower() for m in route.methods
                       if m in ("GET", "POST", "PUT", "PATCH")):
            responses = (spec["paths"].get(route.path, {}).get(method, {})
                         .get("responses", {}))
            success = [code for code in responses if code.startswith("2")]
            schema = None
            for code in success:
                schema = (responses[code].get("content", {})
                          .get("application/json", {}).get("schema"))
                if schema:
                    break
            if not schema:
                missing.append(f"{method.upper()} {route.path} (declared: {success})")
    assert missing == [], f"routes with no success schema: {missing}"


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

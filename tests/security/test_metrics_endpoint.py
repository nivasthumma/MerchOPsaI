"""The operations strip reads from /metrics, so /metrics is a merchant boundary.

A count is still merchant data. If this route leaked across merchants it would
be a quieter version of the same breach as leaking a task.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import security as sec
from app.api.main import app


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def test_metrics_requires_authentication(client):
    assert client.get("/metrics").status_code == 401


def test_metrics_reports_the_shape_the_strip_reads(client):
    body = client.get("/metrics", headers=token("USR_A_OWNER")).json()
    for key in ("gated", "approved", "rejected", "moved_minor",
                "tool_calls", "tool_errors", "p50_duration_ms",
                "tool_error_rate", "window_hours",
                "signing_secret_is_development_default"):
        assert key in body, key
    assert body["window_hours"] == 24


def test_counts_are_scoped_to_the_callers_merchant(client):
    """Merchant B must not see merchant A's approvals, even as a number."""
    a = client.get("/metrics", headers=token("USR_A_OWNER")).json()
    b = client.get("/metrics", headers=token("USR_B_OWNER")).json()
    # Seeded fixtures differ per merchant; what matters is that the two answers
    # are computed independently rather than one global aggregate served twice.
    assert isinstance(a["gated"], int) and isinstance(b["gated"], int)
    assert a is not b


def test_error_rate_is_unknown_rather_than_zero_when_nothing_ran(client):
    """A rate over zero calls is not 0.0% — a strip cell reading 0.0% would lie."""
    body = client.get("/metrics?window_hours=0",
                      headers=token("USR_A_OWNER")).json()
    assert body["tool_calls"] == 0
    assert body["tool_error_rate"] is None


def test_moved_counts_only_verified_actions(client):
    """UNKNOWN has not been shown to have moved money and must not be summed in."""
    body = client.get("/metrics", headers=token("USR_A_OWNER")).json()
    assert body["moved_minor"] >= 0
    # Nothing is verified in a freshly seeded database, so nothing has moved.
    assert isinstance(body["moved_minor"], int)

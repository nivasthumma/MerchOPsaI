"""Operational visibility — ADR-0031.

Distinct from `test_observability.py`, which covers the audit trail: what the
system *decided*, durably and per tenant. These cover what the process is
*doing* — the channel that has to work when the audit trail cannot be written,
which is exactly when something has gone wrong.
"""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.agent.runtime import AgentRuntime, AgentRuntimeError
from app.api import security as sec
from app.api.main import app
from app.audit.trace import correlation_scope, current_correlation_id
from app.llm.deterministic import DeterministicProvider
from app.observability import runtime_metrics as metrics
from app.observability.logs import JsonFormatter, configure_logging, get_logger


@pytest.fixture
def client(db):
    sec.reset_rate_limits()
    metrics.reset()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()
    metrics.reset()


def token(user_id: str) -> dict:
    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def _line(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def _record(level: int = logging.INFO, msg: str = "event", **extra) -> logging.LogRecord:
    r = logging.LogRecord("t", level, __file__, 1, msg, (), None)
    r.__dict__.update(extra)
    return r


# --------------------------------------------------------------- log format
def test_a_log_line_is_one_json_object():
    out = JsonFormatter().format(_record(msg="task_started", task_id="TASK_A"))
    assert "\n" not in out
    body = json.loads(out)
    assert body["event"] == "task_started"
    assert body["level"] == "INFO"
    assert body["task_id"] == "TASK_A"


def test_the_correlation_id_rides_along_without_being_passed():
    """Every line carries it, so nobody has to remember to attach it."""
    with correlation_scope("COR_ABC123"):
        assert _line(_record())["correlation_id"] == "COR_ABC123"
    assert "correlation_id" not in _line(_record())


def test_secrets_do_not_reach_stdout():
    """The same redaction the audit trail uses. Logs are the easier of the two
    to forward somewhere nobody audited."""
    body = _line(_record(api_key="sk-ant-secret", note="key is rzp_test_ABC123"))
    assert body["api_key"] == "[REDACTED]"
    assert "rzp_test_ABC123" not in body["note"]


def test_an_exception_logs_its_type_and_message_but_not_its_frames():
    try:
        raise ValueError("provider said no: rzp_test_LEAK")
    except ValueError:
        import sys
        record = _record(level=logging.ERROR, msg="task_crashed")
        record.exc_info = sys.exc_info()
    body = _line(record)
    assert body["error"]["type"] == "ValueError"
    assert "rzp_test_LEAK" not in body["error"]["message"]
    assert "Traceback" not in json.dumps(body)


def test_a_value_json_cannot_encode_does_not_lose_the_line():
    """A logger that raises destroys the message it was trying to write."""
    from datetime import datetime, timezone
    from app.models import TaskStatus

    body = _line(_record(at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                         status=TaskStatus.COMPLETED))
    assert "2026-01-01" in body["at"]
    assert body["status"]


# ------------------------------------------------------------- request layer
def test_every_response_carries_a_correlation_id(client):
    r = client.get("/health")
    assert r.headers["X-Correlation-ID"]


def test_a_caller_supplied_correlation_id_is_used(client):
    r = client.get("/health", headers={"X-Correlation-ID": "COR-FROM-GATEWAY"})
    assert r.headers["X-Correlation-ID"] == "COR-FROM-GATEWAY"


def test_a_hostile_correlation_id_cannot_inject_into_a_log_line(client):
    """It is a label, and labels come from callers. It is never trusted."""
    r = client.get("/health",
                   headers={"X-Correlation-ID": '","level":"INFO","injected":"yes'})
    got = r.headers["X-Correlation-ID"]
    assert '"' not in got and "," not in got and ":" not in got


def test_an_unrouted_path_is_not_a_metric_label(client):
    """Cardinality is a correctness property: a caller choosing URLs must not be
    choosing our label values."""
    client.get("/definitely/not/a/route")
    rendered = metrics.render()
    assert 'route="<unmatched>"' in rendered
    assert "definitely" not in rendered


def test_a_task_id_in_the_path_becomes_a_template(client):
    """One series per task, forever, is a memory leak wearing a dashboard."""
    client.get("/tasks/TASK_DOESNOTEXIST", headers=token("USR_A_OWNER"))
    rendered = metrics.render()
    assert 'route="/tasks/{task_id}"' in rendered
    assert "TASK_DOESNOTEXIST" not in rendered


def test_requests_are_counted_by_outcome(client):
    client.get("/health")
    client.get("/incidents")                                   # 401
    rendered = metrics.render()
    assert 'merchantops_http_requests_total{method="GET",route="/health",status="200"} 1' in rendered
    assert 'status="401"' in rendered


# ------------------------------------------------------------------ histogram
def test_the_histogram_is_a_valid_prometheus_histogram():
    """Buckets cumulative and non-decreasing, +Inf equal to the count.

    Written because the first implementation was neither: it incremented every
    bucket an observation fitted AND summed them at render, reporting two
    observations where one had occurred.
    """
    metrics.reset()
    for seconds in (0.002, 0.031, 12.5, 45.0):
        metrics.observe("h", seconds, "test", {"route": "/x"})

    buckets = [(l.split('le="')[1].split('"')[0], float(l.split()[-1]))
               for l in metrics.render().splitlines() if l.startswith("h_bucket")]
    counts = [c for _, c in buckets]
    assert counts == sorted(counts), "buckets must be cumulative"
    assert buckets[-1][0] == "+Inf"
    assert buckets[-1][1] == 4
    total = [l for l in metrics.render().splitlines() if l.startswith("h_sum")][0]
    assert abs(float(total.split()[-1]) - 57.533) < 0.001
    metrics.reset()


def test_label_cardinality_is_capped():
    """A metric that exhausts the process is an outage caused by monitoring."""
    metrics.reset()
    for i in range(metrics._LABEL_CAP + 50):
        metrics.counter("c", "test", {"id": str(i)})
    series = [l for l in metrics.render().splitlines() if l.startswith("c_total")]
    assert len(series) <= metrics._LABEL_CAP + 1
    assert any("__overflow__" in l for l in series)
    metrics.reset()


# ------------------------------------------------------------------ the scrape
def test_the_scrape_endpoint_is_absent_when_no_token_is_configured(client, monkeypatch):
    monkeypatch.delenv("METRICS_SCRAPE_TOKEN", raising=False)
    assert client.get("/metrics/prometheus").status_code == 404


def test_the_scrape_endpoint_refuses_a_wrong_token(client, monkeypatch):
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "correct-token")
    assert client.get("/metrics/prometheus").status_code == 401
    assert client.get("/metrics/prometheus",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_the_scrape_endpoint_serves_the_exposition_format(client, monkeypatch):
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "correct-token")
    client.get("/health")
    r = client.get("/metrics/prometheus", headers={"Authorization": "Bearer correct-token"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "# TYPE merchantops_http_requests counter" in r.text


def test_a_user_token_is_not_a_scrape_token(client, monkeypatch):
    """A scraper has no merchant and must not be given one."""
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "correct-token")
    r = client.get("/metrics/prometheus", headers=token("USR_A_OWNER"))
    assert r.status_code == 401


def test_business_metrics_stay_separate_from_process_metrics(client, monkeypatch):
    """`/metrics` is this merchant's counts; `/metrics/prometheus` is the process.
    Neither should start answering the other's question."""
    monkeypatch.setenv("METRICS_SCRAPE_TOKEN", "correct-token")
    business = client.get("/metrics", headers=token("USR_A_OWNER")).json()
    process = client.get("/metrics/prometheus",
                         headers={"Authorization": "Bearer correct-token"}).text
    assert "gated" in business
    assert "merchant" not in process.lower().replace("merchantops", "")


# -------------------------------------------------------------- the agent run
def test_a_run_joins_the_request_that_asked_for_it(db, owner):
    """One id across the HTTP line and the audit rows, or the two cannot be joined."""
    with correlation_scope("COR_REQUESTLEVEL"):
        out = AgentRuntime(db, owner).run("Why did revenue drop this week?")
    assert out.task.id
    from app.models import AuditLog
    rows = db.query(AuditLog).filter(AuditLog.task_id == out.task.id).all()
    assert rows and all(r.correlation_id == "COR_REQUESTLEVEL" for r in rows)


def test_a_run_restores_the_caller_s_correlation_id(db, owner):
    """Clearing it would leave the rest of the request logged as belonging to
    no trace, which is worse than the leak clearing was meant to prevent."""
    with correlation_scope("COR_OUTER"):
        AgentRuntime(db, owner).run("Why did revenue drop this week?")
        assert current_correlation_id() == "COR_OUTER"
    assert current_correlation_id() is None


def test_a_crashed_run_reaches_stdout_not_only_the_database(db, owner, caplog):
    """The audit row is per-tenant and behind authentication. The operator who
    has to fix this reads logs."""
    class _Explodes(DeterministicProvider):
        def turn(self, **kw):
            raise RuntimeError("provider exploded")

    with caplog.at_level(logging.ERROR, logger="merchantops.agent"):
        with pytest.raises(AgentRuntimeError):
            AgentRuntime(db, owner, provider=_Explodes()).run("Why did revenue drop?")

    assert any(r.message == "task_crashed" for r in caplog.records)

"""Runtime metrics — ADR-0031.

Distinct from `app/metrics.py`, and the distinction is the point:

    app/metrics.py            what the BUSINESS did      from the database, per merchant
    app/observability/metrics what the PROCESS is doing   in memory, per instance

One is queried per tenant and answers "how much was recovered". The other is
scraped and answers "is it up, is it slow, is it erroring". Serving them from
one place would mean either scoping infrastructure health to a merchant, which
is meaningless, or exposing another merchant's counts to a scraper, which is a
leak.

## No client library

Prometheus' text exposition format is a documented line protocol, and emitting
it is less code than the adapter around a library would be. The bundle shipped
to the function has a cold-start budget (`api/requirements.txt` explains why
pytest and Streamlit are kept out of it), and a metrics client is a poor way to
spend it.

## Cardinality is a correctness property

A label whose values are unbounded turns a metric into a memory leak: every
distinct value is a permanent series. `/tasks/TASK_9F2A31C0` must be recorded as
`/tasks/{task_id}` or one series becomes one per task, forever. `_LABEL_CAP`
enforces that in the one place it can be enforced, because the alternative is
trusting every future call site.

## Per instance, and honest about it

State is in memory. With more than one worker each holds its own, which is
exactly how Prometheus expects to scrape a multi-instance service — the scraper
aggregates. On Vercel, where instances are created and destroyed per burst,
these counters are close to useless and say so at `/metrics/prometheus`; logs
are the operational channel there.
"""
from __future__ import annotations

import math
import os
import threading
import time
from collections import defaultdict

# Seconds. Chosen for this application's shape rather than copied: a read
# endpoint answers in milliseconds, an agent run takes tens of seconds, and the
# interesting question is which side of that a request fell on.
DEFAULT_BUCKETS = (0.005, 0.025, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)

# Above this many distinct label combinations a metric stops adding new ones and
# folds the rest into `__overflow__`. A metric that loses precision is a
# degraded metric; a metric that exhausts the process is an outage caused by
# monitoring, which is worse than not monitoring.
_LABEL_CAP = 500

_lock = threading.Lock()
_counters: dict[str, dict[tuple, float]] = defaultdict(dict)
_gauges: dict[str, dict[tuple, float]] = defaultdict(dict)
_histograms: dict[str, dict[tuple, list]] = defaultdict(dict)
_meta: dict[str, tuple[str, str]] = {}          # name -> (type, help)
_label_names: dict[str, tuple[str, ...]] = {}

PROCESS_STARTED = time.time()


def _key(labels: dict[str, str] | None) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in (labels or {}).items()))


def _capped(store: dict, name: str, key: tuple) -> tuple:
    series = store[name]
    if key in series or len(series) < _LABEL_CAP:
        return key
    return (("__overflow__", "true"),)


def _register(name: str, kind: str, help_text: str, labels: dict | None) -> None:
    _meta.setdefault(name, (kind, help_text))
    _label_names.setdefault(name, tuple(sorted(labels or {})))


def counter(name: str, help_text: str = "", labels: dict | None = None,
            value: float = 1.0) -> None:
    with _lock:
        _register(name, "counter", help_text, labels)
        k = _capped(_counters, name, _key(labels))
        _counters[name][k] = _counters[name].get(k, 0.0) + value


def gauge(name: str, value: float, help_text: str = "", labels: dict | None = None) -> None:
    with _lock:
        _register(name, "gauge", help_text, labels)
        _gauges[name][_capped(_gauges, name, _key(labels))] = value


def observe(name: str, seconds: float, help_text: str = "",
            labels: dict | None = None, buckets: tuple = DEFAULT_BUCKETS) -> None:
    """Record a duration into a histogram."""
    with _lock:
        _register(name, "histogram", help_text, labels)
        k = _capped(_histograms, name, _key(labels))
        entry = _histograms[name].get(k)
        if entry is None:
            entry = {"buckets": buckets, "counts": [0] * len(buckets),
                     "count": 0, "sum": 0.0}
            _histograms[name][k] = entry
        entry["count"] += 1
        entry["sum"] += seconds
        # The FIRST bucket it fits, and only that one. Buckets are stored
        # non-cumulative and summed at render time; incrementing every matching
        # bucket here as well would count each observation once per bucket it
        # fits, so `le="0.25"` would report two observations where one occurred.
        for i, upper in enumerate(entry["buckets"]):
            if seconds <= upper:
                entry["counts"][i] += 1
                break


def reset() -> None:
    """Test hook. Metric state is process-local and must not leak between tests."""
    with _lock:
        _counters.clear()
        _gauges.clear()
        _histograms.clear()
        _meta.clear()
        _label_names.clear()


# --------------------------------------------------------------------------
# Exposition
# --------------------------------------------------------------------------
def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(key: tuple, extra: tuple = ()) -> str:
    pairs = list(key) + list(extra)
    if not pairs:
        return ""
    return "{" + ",".join(f'{k}="{_escape(str(v))}"' for k, v in pairs) + "}"


def render() -> str:
    """The whole registry in Prometheus text exposition format."""
    lines: list[str] = []
    with _lock:
        for name in sorted(_meta):
            kind, help_text = _meta[name]
            if help_text:
                lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} {kind}")

            if kind == "counter":
                for key, value in sorted(_counters[name].items()):
                    lines.append(f"{name}_total{_render_labels(key)} {value:g}")
            elif kind == "gauge":
                for key, value in sorted(_gauges[name].items()):
                    lines.append(f"{name}{_render_labels(key)} {value:g}")
            else:
                for key, entry in sorted(_histograms[name].items()):
                    cumulative = 0
                    # strict: counts is built as [0] * len(buckets), so a
                    # length mismatch means the histogram is corrupt.
                    for upper, count in zip(entry["buckets"], entry["counts"],
                                            strict=True):
                        cumulative += count
                        lines.append(
                            f'{name}_bucket{_render_labels(key, (("le", _fmt(upper)),))} '
                            f"{cumulative:g}")
                    lines.append(
                        f'{name}_bucket{_render_labels(key, (("le", "+Inf"),))} '
                        f'{entry["count"]:g}')
                    lines.append(f'{name}_sum{_render_labels(key)} {entry["sum"]:g}')
                    lines.append(f'{name}_count{_render_labels(key)} {entry["count"]:g}')
    lines.append("# HELP merchantops_process_uptime_seconds Seconds since this instance started.")
    lines.append("# TYPE merchantops_process_uptime_seconds gauge")
    lines.append(f"merchantops_process_uptime_seconds {time.time() - PROCESS_STARTED:g}")
    return "\n".join(lines) + "\n"


def _fmt(v: float) -> str:
    return "+Inf" if math.isinf(v) else f"{v:g}"


def counters_are_meaningful() -> bool:
    """False where instances do not live long enough to be scraped.

    Reported rather than hidden. On Vercel a counter is reset by the next cold
    start, so a dashboard built on these would be quietly wrong; logs are the
    operational channel there. Saying so is the difference between a limitation
    and a bug nobody has found yet.
    """
    return not os.getenv("VERCEL")

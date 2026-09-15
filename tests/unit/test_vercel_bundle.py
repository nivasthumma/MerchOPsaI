"""The Vercel function installs from pyproject.toml, not api/requirements.txt.

`api/requirements.in` documents the function's runtime set and is locked with
hashes, which makes it look authoritative. Vercel's uv-based Python build reads
`[project].dependencies` in pyproject.toml instead, so a package added only to
the requirements file is never installed: `cryptography` was, and every request
to the deployment failed with ModuleNotFoundError before the app could start.

This keeps the two sets identical, pins included, so the deployed bundle is the
one the lock describes.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _requirements_in() -> dict[str, str]:
    pins = {}
    for line in (ROOT / "api" / "requirements.in").read_text().splitlines():
        m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s#]+)", line.strip())
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def _pyproject() -> dict[str, str]:
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    pins = {}
    for d in deps:
        m = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([^\s;]+)$", d.strip())
        assert m, f"pyproject dependency {d!r} is not an exact pin"
        pins[m.group(1).lower()] = m.group(2)
    return pins


def test_every_function_dependency_is_declared_where_vercel_reads_it():
    missing = sorted(set(_requirements_in()) - set(_pyproject()))
    assert missing == [], (
        f"{missing} are in api/requirements.in but not in pyproject.toml "
        f"[project].dependencies, so Vercel will not install them.")


def test_the_two_sets_pin_the_same_versions():
    req, py = _requirements_in(), _pyproject()
    drift = {k: (req[k], py[k]) for k in req if k in py and req[k] != py[k]}
    assert drift == {}, f"requirements.in vs pyproject.toml pins differ: {drift}"
    assert set(py) == set(req), f"pyproject declares extras: {sorted(set(py) - set(req))}"

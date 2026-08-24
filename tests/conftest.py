"""Shared fixtures. Every test gets a freshly seeded database so that tests
cannot contaminate one another."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scripts.seed_data as seeder
from app.agent.runtime import Principal
from app.db import session_scope


@pytest.fixture(scope="function")
def db():
    seeder.reset_schema()
    data = seeder.build()
    with session_scope() as s:
        for key in ("merchants", "users", "customers", "products",
                    "orders", "payments", "refunds"):
            s.add_all(data[key])
            s.flush()
    with session_scope() as s:
        yield s


@pytest.fixture
def owner() -> Principal:
    return Principal("USR_A_OWNER", "MERCH_A", "owner",
                     ["read:metrics", "read:orders", "action:refund"])


@pytest.fixture
def analyst() -> Principal:
    return Principal("USR_A_ANALYST", "MERCH_A", "analyst",
                     ["read:metrics", "read:orders"])


@pytest.fixture
def owner_b() -> Principal:
    return Principal("USR_B_OWNER", "MERCH_B", "owner",
                     ["read:metrics", "read:orders", "action:refund"])

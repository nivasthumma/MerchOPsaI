"""Shared fixtures. Every test gets a freshly seeded database so that tests
cannot contaminate one another.

That reset drops and recreates the schema, so it must never point at the
database someone is actually using. It used to: `database_url` resolved the
same for the test suite as for the running API, which meant `make test` silently
destroyed every task in development — the page you had open became "Unknown
task" and nobody connected the two events.

The suite now runs against its own database, created on demand. Override with
TEST_DATABASE_URL to point CI somewhere else.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_TEST_URL = (
    "postgresql+psycopg2://merchantops:merchantops@127.0.0.1:5432/merchantops_test"
)


def _ensure_database(url: str) -> None:
    """Create the test database if it is not there yet.

    Connects to the server's default `postgres` database to do it, because you
    cannot create a database from inside the one you are creating.
    """
    from sqlalchemy import create_engine, text

    parts = urlsplit(url)
    target = parts.path.lstrip("/")
    admin = urlunsplit(parts._replace(path="/postgres"))
    eng = create_engine(admin, isolation_level="AUTOCOMMIT", future=True)
    try:
        with eng.connect() as c:
            exists = c.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target}
            ).scalar()
            if not exists:
                # Identifiers cannot be bound as parameters; the name comes from
                # our own configuration, never from a request.
                c.execute(text(f'CREATE DATABASE "{target}"'))
    except Exception as exc:  # pragma: no cover - environment setup
        # Never fall back to the configured application database. Falling back
        # is what made this a bug in the first place: the suite would run, pass,
        # and take development data with it.
        raise RuntimeError(
            f"Could not prepare the test database {target!r}.\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            "The test suite drops and recreates its schema, so it refuses to "
            "share a database with anything else.\n"
            "Fix by granting the role permission once:\n"
            "    sudo -u postgres psql -c 'ALTER ROLE merchantops CREATEDB;'\n"
            "or create it yourself:\n"
            f"    sudo -u postgres createdb -O merchantops {target}\n"
            "or point the suite elsewhere with TEST_DATABASE_URL."
        ) from exc
    finally:
        eng.dispose()


# Set before anything imports app.db — the engine is built once and cached, so
# a later override would be ignored and the suite would run against dev.
_TEST_URL = os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_URL)
_ensure_database(_TEST_URL)
os.environ["DATABASE_URL"] = _TEST_URL

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

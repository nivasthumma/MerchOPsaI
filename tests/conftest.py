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

from sqlalchemy.orm import sessionmaker

import app.db as app_db
import scripts.seed_data as seeder
from app.agent.runtime import Principal
from app.db import get_engine, session_scope

SEEDED_TABLES = ("merchants", "users", "customers", "products",
                 "orders", "payments", "refunds")


@pytest.fixture(scope="session")
def _seeded_schema():
    """Build the dataset ONCE for the whole run.

    It used to be rebuilt per test: drop the schema, recreate it, re-apply the
    audit triggers, re-insert ~600 payments. 0.35s each, 200 tests, and the
    mutation harness runs the whole suite once per mutant -- which worked out at
    eleven thousand reseeds and about an hour of wall clock spent on teardown
    before any assertion ran.

    Isolation is unchanged. Each test now runs inside a transaction that is
    rolled back afterwards, which leaves exactly as little behind as dropping
    the schema did.
    """
    seeder.reset_schema()
    data = seeder.build()
    with session_scope() as s:
        for key in SEEDED_TABLES:
            s.add_all(data[key])
            s.flush()


@pytest.fixture(scope="function")
def db(_seeded_schema, monkeypatch):
    """A session inside a transaction that is always rolled back.

    `join_transaction_mode="create_savepoint"` is what makes this work with code
    that commits. A `session.commit()` inside the application releases a
    SAVEPOINT rather than ending the outer transaction, so committed work is
    visible for the rest of the test and gone after it.

    The application's own session factory is redirected onto the same
    connection. Without that, an endpoint calling `session_scope()` would open a
    second connection, see none of the test's data, and commit its own work
    permanently -- which is the failure mode the per-test reseed was hiding.
    """
    connection = get_engine().connect()
    outer = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False, future=True,
                           join_transaction_mode="create_savepoint")
    monkeypatch.setattr(app_db, "_SessionLocal", factory)

    session = factory()
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def owner() -> Principal:
    return Principal("USR_A_OWNER", "MERCH_A", "owner",
                     ["read:metrics", "read:orders", "action:refund", "action:recover"])


@pytest.fixture
def analyst() -> Principal:
    return Principal("USR_A_ANALYST", "MERCH_A", "analyst",
                     ["read:metrics", "read:orders"])


@pytest.fixture
def approver() -> Principal:
    """A second person at MERCH_A who may approve — MerchantOps §26."""
    return Principal("USR_A_APPROVER", "MERCH_A", "approver",
                     ["read:metrics", "read:orders", "action:refund", "action:recover"])


@pytest.fixture
def owner_b() -> Principal:
    return Principal("USR_B_OWNER", "MERCH_B", "owner",
                     ["read:metrics", "read:orders", "action:refund", "action:recover"])

"""Migrations produce the schema the code expects — ADR-0030.

These tests build real databases rather than using the `db` fixture: migrations
are DDL against a database that does not exist yet, which is not something a
transaction-scoped session can express.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from alembic import command
from app.models import Base

ROOT = Path(__file__).resolve().parents[2]
BASELINE = "dfdcbe8c6ce5"


def _admin_url(url: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path="/postgres"))


def _fresh_database(name: str) -> str:
    """An empty database with the given name, dropped first if it is there.

    Skips only when the role genuinely cannot create databases, which is a
    property of the environment and will not improve on a retry. Everything else
    is retried: a straggling connection makes `DROP DATABASE` fail, and treating
    that as "cannot create a scratch database" would turn the drift guard into a
    test that quietly stops running — the exact failure the guard exists to
    prevent, one level up.
    """
    base = os.environ["DATABASE_URL"]
    admin = create_engine(_admin_url(base), isolation_level="AUTOCOMMIT", future=True)
    last: Exception | None = None
    try:
        for attempt in range(5):
            try:
                with admin.connect() as c:
                    # Stragglers first, or DROP blocks on a connection nobody owns.
                    c.execute(text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :n AND pid <> pg_backend_pid()"), {"n": name})
                    c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
                    c.execute(text(f'CREATE DATABASE "{name}"'))
                break
            except ProgrammingError as exc:
                # No CREATEDB. Retrying cannot help.
                pytest.skip(f"the role cannot create databases: {exc}")
            except OperationalError as exc:
                last = exc
                time.sleep(0.2 * (attempt + 1))
        else:
            raise AssertionError(
                f"could not make a scratch database {name!r} after 5 attempts: {last}")
    finally:
        admin.dispose()
    return urlunsplit(urlsplit(base)._replace(path=f"/{name}"))


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


@pytest.fixture
def migrated_url():
    url = _fresh_database("merchantops_migrationtest")
    command.upgrade(_alembic_config(url), "head")
    yield url


# ------------------------------------------------------------------ drift
def test_head_matches_the_models(migrated_url):
    """The guard that makes every later migration honest.

    Autogenerate is normally a scaffold. Here it is an assertion: a database at
    `head` compared against `app/models.py` must have nothing to say. When it
    does, somebody changed a model and did not write the migration — which used
    to be undetectable, because `create_all` simply built whatever the models
    currently said and no database anyone cared about was ever compared to it.
    """
    engine = create_engine(migrated_url, future=True)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={
                "compare_type": True,
                "include_object":
                    lambda o, n, t, r, c: not (t == "table" and n == "alembic_version"),
            })
            diff = compare_metadata(ctx, Base.metadata)
    finally:
        engine.dispose()

    assert diff == [], (
        "The schema at head does not match app/models.py. Generate the missing "
        "migration with:\n"
        "    alembic revision --autogenerate -m '<what changed>'\n"
        f"Differences: {diff}")


def test_the_drift_guard_is_not_vacuous(migrated_url):
    """A guard that can only pass proves nothing.

    `app/models.py` is not touched — a *copy* of the metadata gains a column,
    which is exactly what changing a model and forgetting the migration looks
    like to the comparison.
    """
    from sqlalchemy import Column, MetaData, String, Table

    modified = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(modified)
    Table("incidents", modified, Column("owner_team", String(64)), extend_existing=True)

    engine = create_engine(migrated_url, future=True)
    try:
        with engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={
                "compare_type": True,
                "include_object":
                    lambda o, n, t, r, c: not (t == "table" and n == "alembic_version"),
            })
            diff = compare_metadata(ctx, modified)
    finally:
        engine.dispose()

    assert len(diff) == 1
    assert diff[0][0] == "add_column"


def test_every_table_the_application_declares_exists(migrated_url):
    engine = create_engine(migrated_url, future=True)
    try:
        present = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert set(Base.metadata.tables) <= present


# --------------------------------------------------- the append-only control
def test_migrating_makes_the_audit_log_append_only(migrated_url):
    """The control is applied by the migration, not by remembering to run a script."""
    engine = create_engine(migrated_url, future=True)
    try:
        with engine.begin() as c:
            c.execute(text(
                "INSERT INTO audit_logs (event_type, payload) VALUES ('probe', '{}'::json)"))
        with engine.connect() as c:
            assert c.execute(text("SELECT count(*) FROM audit_logs")).scalar() == 1

        for verb, sql in (("UPDATE", "UPDATE audit_logs SET event_type = 'tampered'"),
                          ("DELETE", "DELETE FROM audit_logs")):
            with pytest.raises(Exception) as exc, engine.begin() as c:
                c.execute(text(sql))
            assert "append-only" in str(exc.value), f"{verb} was not refused"
    finally:
        engine.dispose()


# ------------------------------------------------- the database that predates us
def test_an_existing_database_is_stamped_rather_than_rebuilt():
    """The upgrade path for every database created before migrations existed.

    It has all the tables and no version row, so alembic reads it as empty and
    tries to create what is already there. `scripts/migrate.py` detects that by
    evidence and stamps the baseline instead.
    """
    from scripts import migrate as migrate_script

    url = _fresh_database("merchantops_legacytest")
    legacy = create_engine(url, future=True)
    try:
        cfg = _alembic_config(url)

        # The legacy database is the schema *as it stood when migrations were
        # introduced*, with no version row. Building it by upgrading to BASELINE
        # and then deleting the stamp says exactly that.
        #
        # It used to be built with `Base.metadata.create_all` — current models —
        # which is a different database: one that already has every table added
        # since. That passed only while no migration created a table, and broke
        # on the first one that did (`event_outbox`, ADR-0033), with a
        # DuplicateTable error that looked like a bug in the new migration
        # rather than in the fixture. A fixture that has to be revisited every
        # time a table is added is a fixture that will eventually be revisited
        # by deleting the assertion.
        command.upgrade(cfg, BASELINE)
        with legacy.begin() as c:
            c.execute(text("DELETE FROM alembic_version"))

        with legacy.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() is None

        # A bare upgrade cannot work here; that is the whole problem.
        with pytest.raises(Exception):
            command.upgrade(cfg, "head")

        # Stamp, then carry on — what scripts/migrate.py does for this state.
        assert migrate_script.BASELINE == BASELINE
        command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")

        with legacy.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() is not None
            ctx = MigrationContext.configure(conn, opts={
                "compare_type": True,
                "include_object":
                    lambda o, n, t, r, c: not (t == "table" and n == "alembic_version"),
            })
            assert compare_metadata(ctx, Base.metadata) == []

        # And it gained the control it never had: create_all alone never
        # installed the triggers.
        with pytest.raises(Exception) as exc, legacy.begin() as c:
            c.execute(text(
                "INSERT INTO audit_logs (event_type, payload) VALUES ('p','{}'::json)"))
            c.execute(text("DELETE FROM audit_logs"))
        assert "append-only" in str(exc.value)
    finally:
        legacy.dispose()


def test_the_baseline_refuses_to_drop_everything():
    """`alembic downgrade base` must not be a way to delete the audit trail."""
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory.from_config(_alembic_config("postgresql://unused/unused"))
    module = scripts.get_revision(BASELINE).module
    with pytest.raises(NotImplementedError):
        module.downgrade()

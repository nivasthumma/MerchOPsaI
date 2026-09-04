"""Alembic environment — ADR-0030.

Two things this file is careful about.

**The URL comes from the application, not from alembic.ini.** `app.config`
already resolves `DATABASE_URL` from the environment; reading it again here
would create a second source of truth for the one question where being wrong is
unrecoverable — you do not get to undo a migration applied to the wrong
database. `alembic.ini` therefore has no `sqlalchemy.url` at all.

**Autogenerate is a drift detector, not just a scaffold.** `target_metadata` is
the application's own `Base.metadata`, and `tests/integration/test_migrations.py`
asserts that a database at `head` produces an empty diff against it. That is
what keeps a hand-written migration honest after somebody adds a column to
`app/models.py` and forgets.
"""
from __future__ import annotations

import logging

from sqlalchemy import engine_from_config, pool

from alembic import context
from app.config import get_settings
from app.models import Base
from app.observability.logs import configure_logging

config = context.config

# NOT `fileConfig(config.config_file_name)`, which is alembic's default. That
# call reconfigures logging for the whole process from alembic.ini and, with
# `disable_existing_loggers` at its default, tears down every handler already
# installed. In a standalone `alembic` invocation that is harmless. In
# `scripts/migrate.py` -- which runs migrations in-process, inside an
# application that has configured its own structured logging -- it silently
# replaces that configuration with an ini file's idea of it.
#
# So the application's logging wins, and alembic's progress comes out in the
# same JSON, with the same correlation id, as everything else.
configure_logging()
logging.getLogger("alembic").setLevel(logging.INFO)

# The application's schema, so `--autogenerate` compares against what the code
# actually declares.
target_metadata = Base.metadata

# An explicitly supplied URL wins — `alembic -x url=...`, or a caller that set
# it on the Config object (the drift test does, to reach a scratch database).
# Otherwise it comes from the application's own settings, so the ordinary case
# cannot migrate a database the application does not talk to.
_explicit = context.get_x_argument(as_dictionary=True).get("url") \
    or config.get_main_option("sqlalchemy.url", None)
config.set_main_option(
    "sqlalchemy.url",
    (_explicit or get_settings().database_url).replace("%", "%%"))


def _include_object(obj, name, type_, reflected, compare_to):
    """Keep alembic's own bookkeeping table out of its own comparisons."""
    return not (type_ == "table" and name == "alembic_version")


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    `alembic upgrade head --sql` is how a migration gets reviewed before it is
    applied to a database anyone cares about, which is the normal path for a
    production change rather than an exotic one.
    """
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=_include_object,
        compare_type=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        _run(connectable)
    finally:
        # Disposed explicitly. Leaving it to garbage collection keeps a
        # connection to the migrated database alive for an indeterminate time,
        # which is invisible in production and blocks `DROP DATABASE` in any
        # test that migrates a scratch database and then replaces it.
        connectable.dispose()


def _run(connectable) -> None:
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=_include_object,
            # Column type changes are a real migration and must not be silently
            # skipped: a String(64) narrowed to String(32) truncates ids.
            compare_type=True,
            # One transaction for the whole upgrade. PostgreSQL has
            # transactional DDL, so a migration that fails halfway leaves the
            # schema exactly as it was rather than half-applied.
            transaction_per_migration=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

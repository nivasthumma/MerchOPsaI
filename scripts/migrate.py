"""Bring a database to the current schema — ADR-0030.

Three states a database can be in, and only one of them is `alembic upgrade`:

    empty                 -> upgrade from scratch
    exists, not stamped   -> stamp the baseline, then upgrade past it
    exists, stamped       -> upgrade

The middle case is the one that matters and the one a bare `alembic upgrade
head` gets wrong. Every database created before migrations existed has all 23
tables and no `alembic_version` row, so alembic believes it is empty and runs
the baseline, which fails on `CREATE TABLE audit_logs` — after the transaction
has already begun. PostgreSQL rolls that back cleanly, so the failure is loud
rather than damaging, but the operator is now staring at an error on a database
that is in fact fine.

Detection is by evidence, not by asking: if `audit_logs` exists and there is no
version row, this database predates migrations and is at the baseline by
definition.

    python scripts/migrate.py            bring to head
    python scripts/migrate.py --status   say where it is, change nothing
    python scripts/migrate.py --sql      print the SQL instead of running it
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from alembic import command
from app.config import get_settings
from app.db import get_engine

ROOT = Path(__file__).resolve().parents[1]
BASELINE = "dfdcbe8c6ce5"


def head_revision() -> str | None:
    """The revision this checkout's migrations end at.

    Read from the script directory rather than hardcoded, so it cannot disagree
    with the migrations actually present. `/ready` compares it against what the
    database is stamped at: a container running code whose schema is ahead of
    the database it was pointed at is alive and unable to serve, which is the
    distinction a readiness probe exists to make.

    Returns None if there is more than one head -- which is what a merge of two
    branches that both added migrations produces (ADR-0041). Reported rather
    than guessed at: with two heads there is no single revision to be at.
    """
    from alembic.script import ScriptDirectory

    heads = ScriptDirectory.from_config(_config()).get_heads()
    return heads[0] if len(heads) == 1 else None


def _config() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    return cfg


def inspect_database() -> tuple[str, str | None]:
    """Return (state, current_revision).

    State is empty | unstamped | current-models | stamped.

    `unstamped` and `current-models` are both "has a schema, has no version
    row", and they need opposite stamps.

    A database that predates ADR-0030 carries the schema as it stood then, so
    the revision it is really at is BASELINE and everything after it still has
    to run. A database built by `scripts/seed_data.py` carries the schema
    `Base.metadata` describes *today*, which is a different thing the moment any
    migration creates a table -- and one does: `event_outbox` (ADR-0033). Stamp
    that one at BASELINE and the upgrade re-creates a table that is already
    there, which is a DuplicateTable error naming the newest migration, on a
    database whose only fault is being newer than the assumption.

    The two are told apart by asking whether every table the models currently
    describe is already present. Only a create_all database can answer yes, and
    `tests/integration/test_migrations.py` is what makes the answer mean
    something: it upgrades a real database to head and asserts no differences
    against `Base.metadata`, so "has every current table" and "is at head" are
    the same statement rather than a hopeful one.
    """
    from app.models import Base

    engine = get_engine()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
        present = set(inspect(conn).get_table_names())
    if current is not None:
        return "stamped", current
    if "audit_logs" not in present:
        return "empty", None
    missing = set(Base.metadata.tables) - present
    return ("unstamped", None) if missing else ("current-models", None)


def main() -> int:
    args = set(sys.argv[1:])
    settings = get_settings()
    # The database, said out loud before anything touches it. A migration
    # applied to the wrong one is not recoverable by running another migration.
    safe_url = settings.database_url.split("@")[-1]
    state, current = inspect_database()

    print(f"database   {safe_url}")
    print(f"state      {state}" + (f" (at {current})" if current else ""))

    if "--status" in args:
        cfg = _config()
        command.history(cfg, indicate_current=True)
        return 0

    cfg = _config()

    if state == "unstamped":
        print(f"stamping   {BASELINE}  (schema predates migrations; not re-creating it)")
        if "--sql" not in args:
            command.stamp(cfg, BASELINE)
    elif state == "current-models":
        print("stamping   head  (schema was built from the models, not by migrations)")
        if "--sql" not in args:
            command.stamp(cfg, "head")

    if "--sql" in args:
        # Review before applying. The offline renderer needs a starting point,
        # and for an existing database that is the baseline it was just told it
        # is at.
        start = current or (BASELINE if state == "unstamped" else None)
        if state == "current-models":
            print("-- already at head; nothing to render")
            return 0
        command.upgrade(cfg, "head", sql=True,
                        revision_range=f"{start}:head" if start else None)
        return 0

    command.upgrade(cfg, "head")
    _, now_at = inspect_database()
    print(f"now at     {now_at}")

    # The control this schema's central claim rests on, checked rather than
    # assumed. A migration that ran and a trigger that fires are different facts.
    from app.db import session_scope
    from scripts.harden_db import verify
    with session_scope() as s:
        results = verify(s)
    for name, ok, detail in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""The restore drill, run every time instead of remembered.

`docs/runbook.md` §4 documents the procedure and records that it was rehearsed
by hand on 2026-09-04. A rehearsal is a date in a document: it says the shape
was right once, and it cannot notice the day a schema change, a new control or
a dependency quietly breaks it.

So the procedure is executed here. Dump the migrated database, restore it into
a fresh one, and assert the three things a restore can silently lose:

    every row                                 -- see below, this is the sharp one
    the append-only control on `audit_logs`   -- the evidence that an action
                                                 was authorised
    the ciphertext in the encrypted columns   -- ADR-0052
    the schema revision                       -- what the code expects

## Why the row counts are asserted

`pg_dump` REFUSES on this database without `--enable-row-security`, because
`FORCE ROW LEVEL SECURITY` is on twenty-odd tables and the dumping role cannot
bypass it. The runbook's documented command therefore does not work, and has
not since RLS landed on 2026-09-05 -- the day after the procedure was rehearsed
by hand.

The flag makes it succeed, and that is the dangerous part. Under RLS the dump
contains what the dumping role can SEE. It is complete here only because
`app.tenancy` lets an unbound session read everything, which that module states
as a deliberate choice and names fail-closed as "a worthwhile second step". Take
that step and every backup silently becomes empty -- restoring cleanly, and
holding nothing.

A partial backup that restores without error is the worst failure available
here, so the counts are compared rather than the exit code trusted. In
production the answer is a backup role with `BYPASSRLS`; the flag is not a
substitute for it, and the runbook says so.

## The one that is new, and the reason this file exists now

Encryption at rest landed on 2026-09-09, and it changed what a backup IS.
A dump is now ciphertext, and the key that reads it is not in the dump: it is
in `ENCRYPTION_KEY`, in the environment. A backup taken faithfully every night
and stored perfectly is **unrecoverable** without it.

`test_a_restore_without_the_key_cannot_read_personal_data` states that as an
executable fact rather than a warning somebody has to read.
"""
from __future__ import annotations

import os
import subprocess
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine, text

from app.crypto import decrypt, is_encrypted

#: Tables carrying rows the seed produces, each under a row-level policy. The
#: point is the COUNT, so these are the ones that actually have rows to lose.
COUNTED = ("customers", "payments", "orders", "agent_actions", "audit_logs")


def _pg_args(url: str) -> list[str]:
    """libpq arguments for a SQLAlchemy URL, without the driver prefix."""
    parts = urlsplit(url.replace("postgresql+psycopg2://", "postgresql://"))
    args = ["--host", parts.hostname or "127.0.0.1",
            "--port", str(parts.port or 5432)]
    if parts.username:
        args += ["--username", parts.username]
    return args


def _env(url: str) -> dict:
    parts = urlsplit(url.replace("postgresql+psycopg2://", "postgresql://"))
    env = dict(os.environ)
    if parts.password:
        env["PGPASSWORD"] = parts.password
    return env


@pytest.fixture(scope="module")
def restored(tmp_path_factory):
    """Migrate a database, seed it, dump it, restore it.

    Built by MIGRATION rather than by `create_all`, unlike the rest of the
    suite. A production database is one alembic has walked, and the two things
    this drill cares most about -- `alembic_version` and the audit triggers --
    exist because a migration created them. Dumping a `create_all` schema would
    prove neither.
    """
    from sqlalchemy.orm import Session

    import scripts.seed_data as seeder
    from alembic import command
    from tests.integration.test_migrations import _alembic_config, _fresh_database

    source = _fresh_database("merchantops_drtest_src")
    command.upgrade(_alembic_config(source), "head")

    src_engine = create_engine(source, future=True)
    with Session(src_engine) as s:
        seeder.insert_all(s, seeder.build())
        s.commit()
    # One audit row, written deliberately. The seed produces none, and both
    # things this drill asserts about `audit_logs` are unprovable on an empty
    # table: a BEFORE UPDATE trigger fires per ROW, so `UPDATE audit_logs`
    # against nothing succeeds and the control looks absent -- and a count that
    # is zero on both sides matches however much was lost.
    with src_engine.begin() as c:
        c.execute(text(
            "INSERT INTO audit_logs (event_type, payload, merchant_id, created_at) "
            "VALUES ('restore_drill', '{}', 'MERCH_A', now())"))

    with src_engine.connect() as c:
        # S608: `COUNTED` is a tuple of literals in this module. Nothing here
        # came from a request.
        before = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()  # noqa: S608
                  for t in COUNTED}
    src_engine.dispose()
    assert sum(before.values()) > 0, "the source database is empty; this proves nothing"

    target = _fresh_database("merchantops_drtest_dst")
    dump = tmp_path_factory.mktemp("dr") / "drill.dump"
    src_db = urlsplit(source.replace("postgresql+psycopg2://", "postgresql://")).path.lstrip("/")
    tgt_db = urlsplit(target.replace("postgresql+psycopg2://", "postgresql://")).path.lstrip("/")

    r = subprocess.run(
        ["pg_dump", *_pg_args(source), "--enable-row-security",
         "--format=custom", "--file", str(dump), src_db],
        capture_output=True, text=True, env=_env(source))
    assert r.returncode == 0, f"pg_dump failed: {r.stderr}"
    assert dump.stat().st_size > 0, "pg_dump produced an empty file"

    subprocess.run(["pg_restore", *_pg_args(target), "--dbname", tgt_db, str(dump)],
                   capture_output=True, text=True, env=_env(target))
    # `pg_restore`'s exit code is not the assertion -- it reports non-fatal
    # notices too. What follows decides whether the restore is usable.
    engine = create_engine(target, future=True)
    try:
        yield engine, before
    finally:
        engine.dispose()


# --------------------------------------------------------------------------
# The evidence
# --------------------------------------------------------------------------
def test_the_audit_log_survives_and_is_still_append_only(restored):
    engine, _ = restored
    """The step the runbook says people skip.

    `pg_restore` restores the trigger function and the triggers -- and a restore
    that silently lost them leaves an audit log that looks identical and is no
    longer append-only. Identical, and no longer evidence.
    """
    with engine.connect() as c:
        # No row-count assertion: the seed writes no audit rows, and the
        # control is the trigger, not the contents. Whether rows survive is
        # covered by `test_every_row_survives_the_dump`.
        triggers = c.execute(text(
            "SELECT tgname FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid "
            "WHERE r.relname = 'audit_logs' AND NOT t.tgisinternal")).scalars().all()
    assert "audit_no_update" in triggers
    assert "audit_no_delete" in triggers

    # Present is not the same as enforced. Prove it by trying.
    for statement in ("UPDATE audit_logs SET action = 'tampered'",
                      "DELETE FROM audit_logs"):
        with pytest.raises(Exception) as e:
            with engine.begin() as c:
                c.execute(text(statement))
        assert "append-only" in str(e.value).lower() or "audit" in str(e.value).lower()


def test_the_schema_revision_comes_back(restored):
    engine, _ = restored
    """A restore that lands on a different revision from the code is a restore
    nobody can start the application against."""
    from alembic.script import ScriptDirectory

    from tests.integration.test_migrations import _alembic_config

    with engine.connect() as c:
        restored_rev = c.execute(
            text("SELECT version_num FROM alembic_version")).scalar_one()
    head = ScriptDirectory.from_config(_alembic_config("postgresql://x/y")).get_current_head()
    assert restored_rev == head, (
        f"restored at {restored_rev}, code expects {head}")


# --------------------------------------------------------------------------
# What encryption changed about a backup
# --------------------------------------------------------------------------
def test_encrypted_columns_are_still_ciphertext_in_the_restore(restored):
    engine, _ = restored
    """A dump carries what is stored, so it carries ciphertext."""
    with engine.connect() as c:
        stored = c.execute(text(
            "SELECT email FROM customers WHERE email IS NOT NULL LIMIT 1")).scalar_one()
    assert is_encrypted(stored), "the restore holds plaintext; the dump was not of an encrypted database"


def test_the_restore_is_readable_with_the_key(restored):
    engine, _ = restored
    """The other half: ciphertext nobody can read is not a recovered system."""
    with engine.connect() as c:
        stored = c.execute(text(
            "SELECT email FROM customers WHERE email IS NOT NULL LIMIT 1")).scalar_one()
    assert "@" in decrypt(stored, table="customers", column="email")


def test_a_restore_without_the_key_cannot_read_personal_data(restored, monkeypatch):
    engine, _ = restored
    """The fact that makes the key part of the backup set.

    A dump taken faithfully every night and stored perfectly is unrecoverable
    without `ENCRYPTION_KEY`. Losing the key loses the data as surely as losing
    the dump -- and unlike the dump, nothing about the database will tell you
    the key is gone until somebody tries to read a name.

    Asserted here so the runbook's instruction to store the key separately, and
    to record which key a dump needs, is backed by something that fails.
    """
    import base64

    with engine.connect() as c:
        stored = c.execute(text(
            "SELECT email FROM customers WHERE email IS NOT NULL LIMIT 1")).scalar_one()

    # A restore performed by a process holding a different key.
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    from app.crypto import EncryptionError

    with pytest.raises(EncryptionError) as e:
        decrypt(stored, table="customers", column="email")
    assert e.value.code == "invalid"


# --------------------------------------------------------------------------
# Completeness — the failure that restores cleanly and holds nothing
# --------------------------------------------------------------------------
def test_every_row_survives_the_dump(restored):
    """Counts, not the exit code.

    Under `FORCE ROW LEVEL SECURITY` a dump contains what the dumping role can
    SEE, so a backup can succeed, restore without a murmur, and be missing
    rows -- or all of them. That is the worst failure available here, because
    every signal says it worked.
    """
    engine, before = restored
    with engine.connect() as c:
        after = {t: c.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar_one()  # noqa: S608
                 for t in COUNTED}
    assert after == before, (
        f"the restore is not the database that was dumped: {before} -> {after}. "
        f"Under row-level security a dump carries what the dumping role can see; "
        f"take a backup with a BYPASSRLS role.")

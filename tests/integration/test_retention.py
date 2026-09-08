"""What gets deleted, what never does, and the guard on both.

`app/retention.py` is the policy. These are the assertions that stop it drifting
from the schema and stop it quietly growing a delete path over the one table
whose value is that it has none.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app import privacy, retention
from app.models import Base


# --------------------------------------------------------------------------
# The policy cannot fall behind the schema
# --------------------------------------------------------------------------
def test_every_table_that_grows_per_event_has_a_policy():
    """A table added with an event-shaped growth pattern and no entry fails here.

    The candidate set is derived from the schema -- a table carrying an
    `occurred_at`, `received_at` or `created_at` is one that accumulates a row
    per event rather than per merchant or per configuration -- so this cannot be
    satisfied by editing the test.
    """
    stamped = {
        t.name for t in Base.metadata.tables.values()
        if {"occurred_at", "received_at"} & {c.name for c in t.columns}
        or ("created_at" in {c.name for c in t.columns} and t.name.endswith(
            ("_logs", "_events", "_results", "_notifications", "_calls",
             "_messages", "_tasks", "_actions")))
    }
    missing = sorted(stamped - set(retention.MAP))
    assert missing == [], (
        f"{missing} accumulate a row per event and have no retention policy. "
        f"Give them one in app.retention.MAP, or record why they are kept -- "
        f"silence is not a decision.")


def test_the_policy_does_not_name_tables_that_do_not_exist():
    real = {t.name for t in Base.metadata.tables.values()}
    assert [t for t in retention.MAP if t not in real] == []


def test_a_prunable_table_names_the_column_its_age_is_measured_from():
    """A period with nothing to measure it against would delete on the first
    sweep or never, and both look like working software."""
    for table in retention.prunable():
        policy = retention.MAP[table]
        assert policy.column, f"{table} has a retention period and no column"
        columns = {c.name for c in Base.metadata.tables[table].columns}
        assert policy.column in columns, (
            f"{table}.{policy.column} does not exist")


def test_every_kept_table_says_why():
    """`days=None` is the answer that needs the most justification, because it
    is also what you get by forgetting."""
    for table in retention.kept():
        why = retention.MAP[table].why
        assert len(why) > 40, f"{table} is kept indefinitely with no real reason"


# --------------------------------------------------------------------------
# The trail
# --------------------------------------------------------------------------
def test_the_audit_log_is_never_pruned():
    """The one this module must never learn to do.

    Deleting from `audit_logs` means suspending the trigger that makes it
    evidence -- a control the restore drill goes out of its way to prove is
    still on. If a retention period ever appears here, it should be because
    somebody argued for it, not because a sweep grew a table list.
    """
    assert retention.MAP["audit_logs"].days is None
    assert "audit_logs" not in retention.prunable()


def test_the_trail_agrees_with_the_privacy_map():
    """Two modules, one position. `app/privacy.py` says the trail is not
    erasable; this says it is not pruned. They must not drift apart."""
    assert "NOT erasable" in privacy.MAP[("audit_logs", "payload")].why
    assert retention.MAP["audit_logs"].days is None


def test_pruning_leaves_the_audit_log_alone(db):
    db.execute(text(
        "INSERT INTO audit_logs (event_type, payload, merchant_id, created_at) "
        "VALUES ('retention_test', '{}', 'MERCH_A', :old)"),
        {"old": datetime.now(UTC) - timedelta(days=3650)})
    db.flush()
    before = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar_one()

    retention.prune(db)

    after = db.execute(text("SELECT COUNT(*) FROM audit_logs")).scalar_one()
    assert after == before, "a ten-year-old audit row was deleted"


# --------------------------------------------------------------------------
# What it does delete
# --------------------------------------------------------------------------
def test_a_published_outbox_row_is_dropped_once_it_is_old(db):
    old = datetime.now(UTC) - timedelta(days=400)
    db.execute(text(
        "INSERT INTO event_outbox (id, event_type, schema_version, payload, "
        "                          payload_hash, occurred_at, published_at, status, attempts) "
        "VALUES ('EVT_OLD', 'x', 1, '{}', 'h1', :o, :o, 'PUBLISHED', 1)"), {"o": old})
    db.flush()

    removed = retention.prune(db)

    assert removed["event_outbox"] >= 1
    assert db.execute(text(
        "SELECT COUNT(*) FROM event_outbox WHERE id = 'EVT_OLD'")).scalar_one() == 0


def test_an_unpublished_outbox_row_is_kept_however_old(db):
    """Age alone is never sufficient. A row nobody consumed is not stale, it is
    unfinished -- and deleting it loses an event that was never delivered."""
    old = datetime.now(UTC) - timedelta(days=400)
    db.execute(text(
        "INSERT INTO event_outbox (id, event_type, schema_version, payload, "
        "                          payload_hash, occurred_at, published_at, status, attempts) "
        "VALUES ('EVT_STUCK', 'x', 1, '{}', 'h2', :o, NULL, 'PENDING', 0)"), {"o": old})
    db.flush()

    retention.prune(db)

    assert db.execute(text(
        "SELECT COUNT(*) FROM event_outbox WHERE id = 'EVT_STUCK'")).scalar_one() == 1


def test_a_recent_published_row_is_kept(db):
    db.execute(text(
        "INSERT INTO event_outbox (id, event_type, schema_version, payload, "
        "                          payload_hash, occurred_at, published_at, status, attempts) "
        "VALUES ('EVT_NEW', 'x', 1, '{}', 'h3', now(), now(), 'PUBLISHED', 1)"))
    db.flush()

    retention.prune(db)

    assert db.execute(text(
        "SELECT COUNT(*) FROM event_outbox WHERE id = 'EVT_NEW'")).scalar_one() == 1


def test_the_period_is_configurable_without_editing_the_policy(db):
    """The numbers are a compliance answer, not an engineering one."""
    at = datetime.now(UTC) - timedelta(days=5)
    db.execute(text(
        "INSERT INTO event_outbox (id, event_type, schema_version, payload, "
        "                          payload_hash, occurred_at, published_at, status, attempts) "
        "VALUES ('EVT_5D', 'x', 1, '{}', 'h4', :o, :o, 'PUBLISHED', 1)"), {"o": at})
    db.flush()

    # Default is 30 days, so five days old survives.
    retention.prune(db)
    assert db.execute(text(
        "SELECT COUNT(*) FROM event_outbox WHERE id = 'EVT_5D'")).scalar_one() == 1

    retention.prune(db, overrides={"event_outbox": 1})
    assert db.execute(text(
        "SELECT COUNT(*) FROM event_outbox WHERE id = 'EVT_5D'")).scalar_one() == 0


def test_it_reports_what_it_removed_per_table(db):
    """"Deleted 40,000 rows" is not something anybody can check."""
    removed = retention.prune(db)
    assert set(removed) == set(retention.prunable())
    assert all(isinstance(v, int) for v in removed.values())


# --------------------------------------------------------------------------
# The worker runs it
# --------------------------------------------------------------------------
def test_the_worker_schedules_retention():
    """A policy nothing runs is a document."""
    import app.worker as worker

    job = next((j for j in worker.build_jobs() if j.name == "retention"), None)
    assert job is not None, "retention is not in the worker's job list"
    # Daily. Anything more frequent is load bought for nothing.
    assert job.interval_seconds >= 3600

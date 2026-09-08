"""The schema reset has to be independent of what is already in the database.

`scripts/seed_data.reset_schema()` is the disposable path: the test suite and
the evaluation runner call it to get a known-empty database. It used to be
`Base.metadata.drop_all`, which drops what the CURRENT branch declares and
leaves everything else standing — and anything left standing with a foreign key
into a table it does drop makes the drop fail.

That is not hypothetical. The test database's name is derived from
`DATABASE_URL`, so every worktree of this repository shares one `<db>_test`,
and two branches with divergent schemas take turns in it. This branch found
`identity_providers`, `roles`, `sso_flows`, `hypotheses` and `evidence_edges`
left behind by the other; the last two carry foreign keys into `incidents`. The
result was 411 fixture errors reading `DependentObjectsStillExist: cannot drop
table incidents`, none of which mention the actual cause, and all of which name
a table that was perfectly fine.

Driven against a scratch database of its own rather than the suite's, because a
test that resets the database the suite is using is a test that breaks every
test after it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

from app.models import Base
from scripts.dbutil import database_name, ensure_database, sibling_url


@pytest.fixture
def scratch_url() -> str:
    """A database this test may destroy, named so the guards agree.

    `_scratch` is in `DISPOSABLE_SUFFIXES`, which is what makes it legitimate
    to drop a schema in it.
    """
    from app.config import get_settings

    base = get_settings().database_url
    url = sibling_url(base, f"{database_name(base)}_reset_scratch")
    ensure_database(url)
    return url


def _table_names(url: str) -> set[str]:
    return set(inspect(create_engine(url, future=True)).get_table_names())


def test_the_reset_drops_tables_this_branch_has_never_heard_of(
        scratch_url, monkeypatch):
    """The failure mode, reproduced: a stray table with a foreign key in.

    `evidence_edges` was one of the real ones. A reset that only knows this
    branch's models leaves it standing, and then cannot drop `incidents`
    because of it.
    """
    from scripts import seed_data

    engine = create_engine(scratch_url, future=True)
    monkeypatch.setattr(seed_data, "get_engine", lambda: engine)

    # Build this branch's schema, then add something it does not know about
    # that points into it — exactly the shape the other branch left behind.
    Base.metadata.create_all(engine)
    with engine.begin() as c:
        c.execute(text("""
            CREATE TABLE stray_edges (
                id TEXT PRIMARY KEY,
                incident_id TEXT NOT NULL REFERENCES incidents(id)
            )
        """))
    assert "stray_edges" in _table_names(scratch_url)

    # `Base.metadata.drop_all` cannot get past this. `reset_schema` must.
    seed_data.reset_schema()

    after = _table_names(scratch_url)
    assert "stray_edges" not in after, (
        "the reset left a table it did not declare, which is what made "
        "dropping `incidents` fail")
    assert "incidents" in after, "the reset did not rebuild this branch's schema"
    assert not (set(Base.metadata.tables) - after), (
        "the reset rebuilt an incomplete schema")

    # And the audit triggers came back with it. `reset_schema` re-applies them
    # because dropping the schema takes them too, and an audit trail that is
    # silently mutable after a reseed is worse than one that was never armed.
    with engine.connect() as c:
        triggers = {r[0] for r in c.execute(text(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
    assert {"audit_no_update", "audit_no_delete"} <= triggers

    # The triggers land on the engine the schema was built on. `reset_schema`
    # used to harden through `session_scope()` while building through
    # `get_engine()` — two ways of reaching a database in one function, and a
    # caller that redirected one got its tables in one place and its audit
    # triggers in another: the schema it asked for left unarmed, and a database
    # it never mentioned altered. This assertion is what caught that.

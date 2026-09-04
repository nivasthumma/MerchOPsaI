"""The second isolation wall — ADR-0046.

This session opened with a mutation-test run that had been killed, leaving

    if False:  # MUTANT

where `app/policy/engine.py` checked that an order belonged to the caller's
merchant. The application started, 700-odd tests passed, and merchant A could
act on merchant B's orders. One `if` was the entire boundary.

The first test below removes the equivalent check from `app/api/main.py._owned`
and asserts the request still fails. That is the whole point of this file: not
that the application filters correctly -- it always did -- but that it is no
longer the only thing that does.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from app.db import session_scope
from app.models import AgentTask, TaskStatus
from app.tenancy import (
    MERCHANT_SCOPED,
    UNSCOPED_TABLES,
    VIA_PARENT,
    covered_tables,
    policy_statements,
    scoped,
    unscoped,
)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient

    from app.api import security as sec
    from app.api.main import app

    sec.reset_rate_limits()
    with TestClient(app) as c:
        yield c
    sec.reset_rate_limits()


def _auth(user_id: str) -> dict:
    from app.api import security as sec

    return {"Authorization": f"Bearer {sec.issue_token(user_id)}"}


def _task_for(merchant_id: str, user_id: str) -> str:
    """A committed task, so a differently-scoped session can try to read it."""
    tid = f"TASK_RLS_{uuid.uuid4().hex[:8].upper()}"
    with unscoped(), session_scope() as s:
        s.add(AgentTask(
            id=tid, merchant_id=merchant_id, user_id=user_id,
            request="isolation probe", status=TaskStatus.COMPLETED,
            agent_version="t", model_version="t", prompt_version="t"))
    return tid


# --------------------------------------------------------------------------
# The one that matters
# --------------------------------------------------------------------------
def test_the_database_refuses_when_the_application_forgets(client, monkeypatch):
    """`_owned` with its merchant check removed — the mutant, at the route.

    Before row-level security this returned merchant B's task to merchant A with
    a 200. Now `s.get()` finds nothing, because the row is not visible to a
    session bound to A, and the same 404 comes out of a different mechanism.
    """
    import app.api.main as main

    b_task = _task_for("MERCH_B", "USR_B_OWNER")

    def _owned_without_the_check(s, task_id, principal):
        task = s.get(AgentTask, task_id)
        if task is None:
            raise main.HTTPException(404, "Unknown task.")
        # if task.merchant_id != principal.merchant_id:   <-- the mutant
        return task

    monkeypatch.setattr(main, "_owned", _owned_without_the_check)

    r = client.get(f"/tasks/{b_task}", headers=_auth("USR_A_OWNER"))
    assert r.status_code == 404, (
        "with the application check removed, merchant A read merchant B's task; "
        "the database is not filtering")

    try:
        with unscoped(), session_scope() as s:
            s.execute(text("DELETE FROM agent_tasks WHERE id = :i"), {"i": b_task})
    finally:
        pass


def test_the_same_request_succeeds_for_the_owner(client, monkeypatch):
    """The check above must not pass merely because everything 404s."""
    import app.api.main as main

    a_task = _task_for("MERCH_A", "USR_A_OWNER")

    def _owned_without_the_check(s, task_id, principal):
        task = s.get(AgentTask, task_id)
        if task is None:
            raise main.HTTPException(404, "Unknown task.")
        return task

    monkeypatch.setattr(main, "_owned", _owned_without_the_check)
    try:
        r = client.get(f"/tasks/{a_task}", headers=_auth("USR_A_OWNER"))
        assert r.status_code == 200
        assert r.json()["id"] == a_task
    finally:
        with unscoped(), session_scope() as s:
            s.execute(text("DELETE FROM agent_tasks WHERE id = :i"), {"i": a_task})


# --------------------------------------------------------------------------
# The boundary itself
# --------------------------------------------------------------------------
def test_a_query_with_no_where_clause_sees_one_merchant():
    naked = "SELECT DISTINCT merchant_id FROM payments ORDER BY 1"

    with unscoped(), session_scope() as s:
        everyone = [r[0] for r in s.execute(text(naked))]
    assert len(everyone) > 1, "the fixture needs more than one merchant to mean anything"

    with scoped("TEN_KETTLE", "MERCH_A"), session_scope() as s:
        assert [r[0] for r in s.execute(text(naked))] == ["MERCH_A"]

    with scoped("TEN_NORTHWIND", "MERCH_B"), session_scope() as s:
        assert [r[0] for r in s.execute(text(naked))] == ["MERCH_B"]


def test_naming_another_merchant_explicitly_does_not_help():
    with scoped("TEN_KETTLE", "MERCH_A"), session_scope() as s:
        n = s.execute(text(
            "SELECT count(*) FROM payments WHERE merchant_id = 'MERCH_B'")).scalar()
    assert n == 0


def test_a_child_table_inherits_its_parents_boundary():
    """`tool_calls` has no merchant of its own. The policy joins to `agent_tasks`,
    which is itself filtered -- so the child inherits rather than restates."""
    with unscoped(), session_scope() as s:
        total = s.execute(text("SELECT count(*) FROM tool_calls")).scalar()
    with scoped("TEN_NORTHWIND", "MERCH_B"), session_scope() as s:
        mine = s.execute(text("SELECT count(*) FROM tool_calls")).scalar()
    assert mine < total or total == 0


def test_the_boundary_applies_to_writes(db):
    """`WITH CHECK`, not just `USING`. A row cannot be inserted INTO another
    merchant either -- otherwise the read boundary could be walked around by
    writing a row and reading it back.

    On its own connection: a policy violation aborts the transaction it happens
    in, and the suite runs every test inside one shared transaction it rolls
    back at the end. Borrowing that one would take the rest of the test with it.
    """
    from sqlalchemy.exc import DatabaseError

    from app.db import get_engine
    from app.tenancy import MERCHANT_GUC, TENANT_GUC

    with get_engine().connect() as conn:
        conn.exec_driver_sql(
            "SELECT set_config(%s, %s, true), set_config(%s, %s, true)",
            (TENANT_GUC, "TEN_KETTLE", MERCHANT_GUC, "MERCH_A"))
        with pytest.raises(DatabaseError) as exc:
            conn.exec_driver_sql("""
                INSERT INTO agent_tasks (id, merchant_id, user_id, request, status,
                    agent_version, findings, is_replay, tool_call_count,
                    llm_turn_count, model_requires_human, attempts, created_at)
                VALUES ('TASK_RLS_WRITE', 'MERCH_B', 'USR_B_OWNER', 'x', 'COMPLETED',
                        'v', '[]'::json, false, 0, 0, false, 0, now())
            """)
        conn.rollback()
    assert "policy" in str(exc.value).lower()


def test_an_unbound_session_is_unrestricted_and_that_is_the_documented_limit():
    """Stated as a test so the limit cannot be quietly forgotten. The wall is on
    the authenticated request path; background code is the trusted plane."""
    with unscoped(), session_scope() as s:
        merchants = s.execute(text("SELECT count(DISTINCT merchant_id) FROM payments")).scalar()
    assert merchants > 1


# --------------------------------------------------------------------------
# Coverage, and the two copies of the DDL
# --------------------------------------------------------------------------
def test_every_table_with_a_merchant_is_covered():
    """A table added with a merchant_id and no policy is a hole. This is the
    test that notices."""
    from app.models import Base

    have_merchant = {
        name for name, tbl in Base.metadata.tables.items()
        if "merchant_id" in tbl.columns
    }
    missing = have_merchant - set(MERCHANT_SCOPED)
    assert missing == set(), (
        f"{sorted(missing)} carry a merchant_id and no row-level policy. Add "
        f"them to app.tenancy.MERCHANT_SCOPED, or to UNSCOPED_TABLES with a "
        f"reason.")


def test_every_table_is_either_covered_or_deliberately_not():
    from app.models import Base

    known = set(covered_tables()) | set(UNSCOPED_TABLES) | {"alembic_version"}
    unaccounted = set(Base.metadata.tables) - known
    assert unaccounted == set(), (
        f"{sorted(unaccounted)} are neither covered nor listed as deliberately "
        f"uncovered. Silence is not a decision.")


def test_the_policies_are_actually_on_the_database():
    with unscoped(), session_scope() as s:
        rows = {r[0] for r in s.execute(text(
            "SELECT tablename FROM pg_policies WHERE schemaname = 'public'"))}
    assert set(covered_tables()) <= rows


def test_every_policy_is_forced_not_merely_enabled():
    """The application role OWNS these tables, and PostgreSQL exempts an owner
    from its own policies without FORCE. Enabling alone produces a control that
    reports as present and filters nothing."""
    with unscoped(), session_scope() as s:
        unforced = [r[0] for r in s.execute(text("""
            SELECT relname FROM pg_class
            WHERE relrowsecurity AND NOT relforcerowsecurity
        """))]
    assert unforced == []


def test_the_migration_and_the_live_ddl_agree():
    """Two copies exist on purpose: a migration that imports live code stops
    being a snapshot of the schema at a point in time, and `harden_db` needs the
    live one because `create_all` does not carry policies. This is what stops
    them drifting."""
    import importlib.util
    import pathlib

    path = next(pathlib.Path("alembic/versions").glob("*_row_level_security.py"))
    spec = importlib.util.spec_from_file_location("rls_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module.MERCHANT_SCOPED) == set(MERCHANT_SCOPED)
    assert module.VIA_PARENT == VIA_PARENT
    assert set(module.ALL_TABLES) == set(covered_tables())
    assert policy_statements(), "the live DDL generator produced nothing"

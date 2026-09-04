"""Binding a request's principal to its database transaction.

Isolation in this system has always been checked in Python: every route resolves
the principal server-side and every query carries `WHERE merchant_id = ...`. That
is correct, and it is one wall. The session that produced this module opened with
a mutation-test run that had been killed, leaving

    if False:  # MUTANT

where the cross-merchant check used to be. The application started, the suite
passed, and merchant A could act on merchant B's orders. One `if` was the whole
boundary.

So this is the second wall. `app.tenancy` binds the authenticated principal to
the transaction, PostgreSQL row-level security filters every table against it,
and a query that forgets its `WHERE` returns nothing rather than everything.

## How the binding happens

A context variable, set by `current_principal` when a request authenticates, and
read by `session_scope` when it opens a transaction. Nothing in a route changes,
which is the point -- a control that each of forty-eight routes has to remember
is a control that forty-seven of them have.

## What this does NOT claim

**An unbound session is unrestricted, not blocked.** When no principal is bound,
`app.merchant_id` is empty and every policy passes. That is a deliberate choice
and it bounds what this control is worth, so it is stated here rather than
discovered later.

The alternative -- fail closed, so an unbound session sees nothing -- is the
stronger control and would require every sweep, script, migration and seeder to
declare itself, because background work in this system genuinely is
cross-merchant: reconciliation settles every unsettled action, the drain
publishes every pending event, detection enumerates merchants. Roughly thirty
call sites, each of which would fail closed at some later date if somebody added
a thirty-first and forgot.

So the wall stands exactly where the risk is: **the authenticated request path**.
Every route resolves a principal, every principal is bound here, and from that
point a query that forgets its `WHERE merchant_id` returns nothing instead of
everything. That is where the mutant lived, and it is where forty-eight routes
each have to remember the same clause.

Background code is the trusted plane, reviewed as such. Making it fail closed is
a worthwhile second step and is not this one.

## `unscoped()` is for shedding a binding, not for gaining rights

It exists because the worker binds a principal to run one merchant's task and
then runs sweeps that must see all of them. Without it those sweeps would
inherit the last task's merchant and silently do a fraction of their work.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

#: Postgres settings the policies read. Namespaced so `current_setting` can be
#: called with `missing_ok=true` and return NULL rather than raising.
#:
#: Empty means unrestricted. There is deliberately no separate "bypass" flag: a
#: flag settable by the same connection that the policy consults is not a
#: boundary, and having one would suggest a stronger guarantee than an empty
#: setting already provides.
TENANT_GUC = "app.tenant_id"
MERCHANT_GUC = "app.merchant_id"


@dataclass(frozen=True)
class Scope:
    tenant_id: str | None
    merchant_id: str | None

    @property
    def is_bound(self) -> bool:
        return self.merchant_id is not None


UNBOUND = Scope(None, None)

_CURRENT: ContextVar[Scope] = ContextVar("merchantops_scope", default=UNBOUND)


def current_scope() -> Scope:
    return _CURRENT.get()


def bind(tenant_id: str | None, merchant_id: str | None):
    """Bind a principal for this context. Returns the token to reset with."""
    return _CURRENT.set(Scope(tenant_id, merchant_id))


def reset(token) -> None:
    _CURRENT.reset(token)


@contextmanager
def scoped(tenant_id: str | None, merchant_id: str | None) -> Iterator[Scope]:
    token = bind(tenant_id, merchant_id)
    try:
        yield current_scope()
    finally:
        reset(token)


@contextmanager
def unscoped() -> Iterator[Scope]:
    """Shed any binding for this block.

    Used where work legitimately crosses merchants *after* something bound a
    principal -- the worker runs one merchant's task, then sweeps that must see
    every merchant. Without this those sweeps inherit the last task's merchant
    and quietly do a fraction of their work.
    """
    token = _CURRENT.set(UNBOUND)
    try:
        yield UNBOUND
    finally:
        _CURRENT.reset(token)


def apply(connection, scope: Scope | None = None) -> None:
    """Push the scope onto a transaction with SET LOCAL.

    `SET LOCAL` and not `SET`: the setting has to die with the transaction. On a
    pooled connection a plain `SET` would outlive the request that made it and
    apply to whichever request got that connection next -- one merchant's scope
    silently serving another's traffic, which is worse than having no scope at
    all.
    """
    scope = current_scope() if scope is None else scope

    # `set_config(..., is_local => true)` rather than `SET LOCAL`: the value
    # comes from a bearer token's subject, `SET LOCAL x = $1` is not valid
    # syntax, and interpolating a principal-derived string into DDL-ish SQL to
    # enforce isolation would be a fine joke at this system's expense.
    connection.exec_driver_sql(
        "SELECT set_config(%s, %s, true), set_config(%s, %s, true)",
        (TENANT_GUC, scope.tenant_id or "", MERCHANT_GUC, scope.merchant_id or ""))


# --------------------------------------------------------------------------
# Which tables the boundary covers
# --------------------------------------------------------------------------
#: Tables carrying a merchant_id of their own.
MERCHANT_SCOPED: tuple[str, ...] = (
    "agent_actions", "agent_tasks", "approvals", "audit_logs", "customers",
    "event_outbox", "evidence_edges", "hypotheses", "incidents", "notifications",
    "operator_notifications", "orders", "payment_links", "payments", "products",
    "recovery_candidates", "recovery_plans", "refunds", "users", "webhook_events",
)

#: Child tables with no merchant of their own, filtered through the parent that
#: has one. The parent is itself under RLS, so the subquery sees only in-scope
#: rows and the child inherits the boundary rather than restating it.
VIA_PARENT: dict[str, tuple[str, str]] = {
    "tool_calls": ("agent_tasks", "task_id"),
    "agent_messages": ("agent_tasks", "task_id"),
    "approval_signatures": ("approvals", "approval_id"),
    "incident_evidence": ("incidents", "incident_id"),
}

#: Tables owned by a tenant rather than a merchant. A role belongs to the tenant
#: that defined it and is used by every merchant under it.
TENANT_SCOPED: tuple[str, ...] = ("roles",)

#: Tenant-owned children, filtered through their tenant-owned parent.
TENANT_VIA_PARENT: dict[str, tuple[str, str]] = {
    "role_permissions": ("roles", "role_id"),
}

#: Deliberately uncovered. `evaluation_results` is a scenario run,
#: `worker_heartbeats` is a process saying it is alive, and `permissions` is a
#: catalogue of names every tenant draws from: platform data with no merchant or
#: tenant to scope it to. Inventing one would be inventing a relationship that
#: does not exist. Listed rather than omitted so the gap is a decision.
UNSCOPED_TABLES: tuple[str, ...] = (
    "evaluation_results", "worker_heartbeats", "permissions",
    # A denylist of token ids, consulted on every request before the principal
    # -- and therefore before there is a scope to filter by. Scoping it would
    # make revocation depend on the very session it is deciding about.
    "revoked_tokens")

_UNRESTRICTED = "coalesce(current_setting('app.merchant_id', true), '') = ''"
_TENANT_UNRESTRICTED = "coalesce(current_setting('app.tenant_id', true), '') = ''"

POLICY_NAME = "merchant_isolation"


def policy_statements() -> list[str]:
    """The DDL that puts the boundary in the database.

    Used by `scripts/harden_db.py`, because `seed_data.reset_schema` builds the
    schema with `create_all` from `Base.metadata` -- and a policy is not in
    `Base.metadata`. Without this, every database built the fast way would have
    the tables and none of the boundary, which is exactly how this was
    discovered.

    The migration keeps its own frozen copy, as the audit triggers do: a
    migration that imports live code stops being a snapshot of the schema at a
    point in time. `tests/integration/test_tenancy.py` asserts the two agree.
    """
    out: list[str] = []

    def policy(table: str, predicate: str) -> None:
        out.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        # FORCE, because the application role owns these tables and PostgreSQL
        # exempts an owner from its own policies without it. Enabling alone
        # produces a control that reports as present and filters nothing.
        out.append(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        out.append(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}")
        # FOR ALL with WITH CHECK, so writes are bounded too: a row cannot be
        # inserted or updated INTO another merchant either.
        out.append(f"CREATE POLICY {POLICY_NAME} ON {table} "
                   f"FOR ALL USING ({predicate}) WITH CHECK ({predicate})")

    for table in MERCHANT_SCOPED:
        policy(table, f"{_UNRESTRICTED} OR merchant_id = "
                      f"current_setting('app.merchant_id', true)")
    for table, (parent, fk) in VIA_PARENT.items():
        policy(table, f"{_UNRESTRICTED} OR EXISTS (SELECT 1 FROM {parent} p "
                      f"WHERE p.id = {table}.{fk})")
    # By TENANT, not merchant: a tenant owns one or more merchants (§11), and a
    # principal scoped to one of them must still be able to read the merchant
    # row it is attached to.
    for table in TENANT_SCOPED:
        policy(table, f"{_TENANT_UNRESTRICTED} OR tenant_id = "
                      f"current_setting('app.tenant_id', true)")
    for table, (parent, fk) in TENANT_VIA_PARENT.items():
        policy(table, f"{_TENANT_UNRESTRICTED} OR EXISTS (SELECT 1 FROM {parent} p "
                      f"WHERE p.id = {table}.{fk})")
    policy("merchants", f"{_TENANT_UNRESTRICTED} OR tenant_id = "
                        f"current_setting('app.tenant_id', true)")
    policy("tenants", f"{_TENANT_UNRESTRICTED} OR id = "
                      f"current_setting('app.tenant_id', true)")
    return out


def covered_tables() -> tuple[str, ...]:
    return (*MERCHANT_SCOPED, *VIA_PARENT, *TENANT_SCOPED, *TENANT_VIA_PARENT,
            "merchants", "tenants")

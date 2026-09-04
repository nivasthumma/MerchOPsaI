"""row level security

The second isolation wall (ADR-0046). Every table carrying a merchant is
filtered against `app.merchant_id`, which `app/tenancy.py` pushes onto the
transaction from the authenticated principal. A query that forgets its
`WHERE merchant_id` returns nothing instead of another merchant's rows.

FORCE ROW LEVEL SECURITY, not merely ENABLE: the application role owns these
tables, and PostgreSQL exempts a table's owner from its own policies unless
forced. Without FORCE this migration would apply cleanly and do nothing at all,
which is the worst outcome available -- a control that reports as present and
filters nothing.

An EMPTY `app.merchant_id` passes every policy. That is the unauthenticated and
background plane -- sweeps, the drain, migrations, the seeder -- and it bounds
what this control is worth: it protects the authenticated request path, which is
where forty-eight routes each have to remember the same clause, and it is not a
capability boundary against code running in this process. ADR-0046 says so at
more length.

Child tables (`tool_calls`, `agent_messages`, `approval_signatures`,
`incident_evidence`) carry no merchant of their own and are filtered by an
EXISTS against their parent. The parent is itself under RLS, so the subquery
sees only in-scope rows and the child inherits the boundary rather than
restating it.

`evaluation_results` and `worker_heartbeats` are deliberately left alone. They
are platform data -- a scenario run and a process saying it is alive -- with no
merchant to scope them to, and inventing one would be inventing a relationship
that does not exist.

LOCKING: ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY and CREATE POLICY each
take a brief ACCESS EXCLUSIVE lock on the table and rewrite nothing.

Revision ID: f692a958917d
Revises: dcb6f90706de
Created: 2026-09-05 02:05:01.193442
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'f692a958917d'
down_revision = 'dcb6f90706de'
branch_labels = None
depends_on = None




# Tables with a merchant_id column of their own.
MERCHANT_SCOPED = (
    "agent_actions", "agent_tasks", "approvals", "audit_logs", "customers",
    "event_outbox", "evidence_edges", "hypotheses", "incidents", "notifications",
    "operator_notifications", "orders", "payment_links", "payments", "products",
    "recovery_candidates", "recovery_plans", "refunds", "users", "webhook_events",
)

# Child tables, filtered through the parent that does carry one.
VIA_PARENT = {
    "tool_calls": ("agent_tasks", "task_id"),
    "agent_messages": ("agent_tasks", "task_id"),
    "approval_signatures": ("approvals", "approval_id"),
    "incident_evidence": ("incidents", "incident_id"),
}

UNRESTRICTED = "coalesce(current_setting('app.merchant_id', true), '') = ''"
TENANT_UNRESTRICTED = "coalesce(current_setting('app.tenant_id', true), '') = ''"

ALL_TABLES = (*MERCHANT_SCOPED, *VIA_PARENT, "merchants", "tenants")


def _policy(table: str, predicate: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # Without FORCE the owner -- which is the application role -- is exempt and
    # every policy below is decoration.
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    # FOR ALL with WITH CHECK, so the boundary applies to writes too: a row
    # cannot be inserted or updated INTO another merchant either.
    op.execute(f"""
        CREATE POLICY merchant_isolation ON {table}
        FOR ALL USING ({predicate}) WITH CHECK ({predicate})
    """)


def upgrade() -> None:
    for table in MERCHANT_SCOPED:
        _policy(table, f"{UNRESTRICTED} OR merchant_id = current_setting('app.merchant_id', true)")

    for table, (parent, fk) in VIA_PARENT.items():
        _policy(table, f"""{UNRESTRICTED} OR EXISTS (
            SELECT 1 FROM {parent} p WHERE p.id = {table}.{fk})""")

    # A tenant may own several merchants and a principal is authorised for one
    # of them, so this is scoped by TENANT rather than by merchant -- otherwise
    # a user could not read the merchant they are attached to alongside its
    # siblings, which §11 says a tenant owns.
    _policy("merchants", f"{TENANT_UNRESTRICTED} OR tenant_id = current_setting('app.tenant_id', true)")
    _policy("tenants", f"{TENANT_UNRESTRICTED} OR id = current_setting('app.tenant_id', true)")


def downgrade() -> None:
    for table in ALL_TABLES:
        op.execute(f"DROP POLICY IF EXISTS merchant_isolation ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

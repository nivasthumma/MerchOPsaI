"""authoritative provider mappings, and reconciliation as durable work

Two changes, both from the enterprise production plan.

**P0-02 — authoritative provider mapping.** `payments.external_payment_id`
resolved an internal id to a provider id and could not promise anything about
the result: nothing stopped two internal payments naming the same `pay_...`,
and the column recorded neither the provider nor the *environment* the id
belonged to. A Test Mode id and a live id are indistinguishable as strings, so
which universe an id lived in was decided by whichever credentials the process
happened to hold at call time.

`provider_mappings` puts provider and environment on the row and holds both
directions UNIQUE:

    uq_mapping_payment_provider_env   resolution is a function — one internal
                                      payment, one external id per universe
    uq_mapping_external_identity      and no external object is claimed by two
                                      internal payments

The existing column is backfilled into the new table rather than dropped. It
remains the mock adapter's own store — the mock *is* the provider, so it holding
provider-side state is correct — and `app.integrations.mapping.check_consistency`
asserts the two agree, which `/readiness` reports.

**P0-04 / P0-15 — UNKNOWN is unresolved financial work.** Escalation was
`verify_attempts >= max_attempts`, re-derived independently by the sweep and by
the operator queue, with no record of *when* the system gave up. Four columns on
`agent_actions` make the reconciliation workflow a stored fact: `escalated`,
`escalated_at`, `last_verified_at`, `next_verify_at`.

`next_verify_at` is backfilled to NULL, which the sweep reads as "eligible now" —
so existing unsettled actions are picked up on the next pass exactly as they
were before this migration, and only then acquire a schedule.

Locking: three ADD COLUMNs with no default rewrite (PostgreSQL 11+ adds a
nullable column, and a `false` default, without rewriting the table) and one
CREATE TABLE. Transactional, per ADR-0030.

Revision ID: c3f18a4d7b62
Revises: d09395a87106
Created: 2026-09-07
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c3f18a4d7b62'
down_revision = 'd09395a87106'
branch_labels = None
depends_on = None


def _existing(bind) -> tuple[set[str], set[str]]:
    """What is already there.

    Not defensive coding for its own sake. `scripts/migrate.py` supports a real
    upgrade path where a database was built by `Base.metadata.create_all` — the
    way this project created schemas before migrations existed — and then
    *stamped* at the baseline rather than rebuilt. Such a database can already
    hold objects introduced after the revision it is stamped at, because
    `create_all` builds whatever the models said on the day it ran.

    A bare CREATE TABLE fails on it, and failing is the wrong answer: the
    deployment is legitimate and the end state is identical. So the objects this
    migration introduces are created only if absent. Shape is not assumed —
    `test_head_matches_the_models` compares the final schema against the models
    and would fail on an object that exists with the wrong columns, so skipping
    can hide nothing.
    """
    insp = sa.inspect(bind)
    tables = set(insp.get_table_names())
    columns = ({c["name"] for c in insp.get_columns("agent_actions")}
               if "agent_actions" in tables else set())
    return tables, columns


def upgrade() -> None:
    bind = op.get_bind()
    tables, action_columns = _existing(bind)

    if "provider_mappings" not in tables:
        _create_provider_mappings()

    _backfill_mappings()
    _add_reconciliation_columns(action_columns)


def _create_provider_mappings() -> None:
    op.create_table(
        "provider_mappings",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("merchant_id", sa.String(64), sa.ForeignKey("merchants.id"),
                  nullable=False),
        sa.Column("payment_id", sa.String(64), sa.ForeignKey("payments.id"),
                  nullable=False),
        sa.Column("provider", sa.String(32), nullable=False,
                  server_default="razorpay"),
        sa.Column("environment", sa.String(16), nullable=False,
                  server_default="test"),
        sa.Column("external_payment_id", sa.String(64), nullable=False),
        # `sa.Enum(..., native_enum=False)` renders as VARCHAR plus a CHECK,
        # matching `MappingStatus` in the models exactly. A hand-written
        # `String(16)` here would pass every test and then fail the schema-drift
        # guard, which is the guard doing its job.
        sa.Column("status",
                  sa.Enum("ACTIVE", "RETIRED", name="mappingstatus",
                          native_enum=False),
                  nullable=False, server_default="ACTIVE"),
        sa.Column("source", sa.String(32), nullable=False, server_default="seed"),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # An environment outside the known set resolves to nothing and would sit
        # in the table looking like a mapping. Refused by the database, because
        # the application is not the only thing that writes here — a seed
        # script, a spike and an operator all do.
        sa.CheckConstraint("environment IN ('test', 'live')",
                           name="ck_mapping_environment"),
        sa.UniqueConstraint("payment_id", "provider", "environment",
                            name="uq_mapping_payment_provider_env"),
        sa.UniqueConstraint("provider", "environment", "external_payment_id",
                            name="uq_mapping_external_identity"),
    )
    op.create_index("ix_provider_mappings_merchant_id", "provider_mappings",
                    ["merchant_id"])
    op.create_index("ix_provider_mappings_payment_id", "provider_mappings",
                    ["payment_id"])
    op.create_index("ix_provider_mappings_status", "provider_mappings", ["status"])
    op.create_index("ix_mapping_lookup", "provider_mappings",
                    ["merchant_id", "provider", "environment"])


def _backfill_mappings() -> None:
    # Backfill. `environment='test'` is the honest reading of every mapping that
    # exists today: this build ships a mock adapter and a Test Mode adapter, and
    # has never had live credentials. Claiming otherwise for rows whose universe
    # nobody recorded would be inventing the one fact this table exists to hold.
    #
    # DISTINCT ON drops duplicate external ids rather than failing the
    # migration on them. A pre-existing duplicate is a data defect the old
    # column permitted; the surviving row is the lowest payment id, and
    # `check_consistency` reports every payment left without a mapping so the
    # loss is visible rather than silent.
    op.execute("""
        INSERT INTO provider_mappings
            (id, merchant_id, payment_id, provider, environment,
             external_payment_id, status, source, created_at)
        SELECT DISTINCT ON (COALESCE(external_provider, 'razorpay'), external_payment_id)
               'PMP_' || upper(substr(md5(id), 1, 12)),
               merchant_id, id,
               COALESCE(external_provider, 'razorpay'), 'test',
               external_payment_id, 'ACTIVE', 'backfill', now()
          FROM payments
         WHERE external_payment_id IS NOT NULL
         ORDER BY COALESCE(external_provider, 'razorpay'), external_payment_id, id
           -- Idempotent against a stamped legacy database that may already
           -- carry rows, and against a re-run. The identity constraint is what
           -- decides, not a prior SELECT.
        ON CONFLICT DO NOTHING
    """)


def _add_reconciliation_columns(existing: set[str]) -> None:
    new_columns = {
        "escalated": sa.Column("escalated", sa.Boolean(), nullable=False,
                               server_default=sa.false()),
        "escalated_at": sa.Column("escalated_at", sa.DateTime(timezone=True),
                                  nullable=True),
        "last_verified_at": sa.Column("last_verified_at", sa.DateTime(timezone=True),
                                      nullable=True),
        "next_verify_at": sa.Column("next_verify_at", sa.DateTime(timezone=True),
                                    nullable=True),
    }
    for name, column in new_columns.items():
        if name not in existing:
            op.add_column("agent_actions", column)

    insp = sa.inspect(op.get_bind())
    indexes = {i["name"] for i in insp.get_indexes("agent_actions")}
    if "ix_agent_actions_escalated" not in indexes:
        op.create_index("ix_agent_actions_escalated", "agent_actions", ["escalated"])
    if "ix_agent_actions_next_verify_at" not in indexes:
        op.create_index("ix_agent_actions_next_verify_at", "agent_actions",
                        ["next_verify_at"])

    # Preserve the escalation the old threshold implied. Without this, every
    # action that had already exhausted its attempts would come back into the
    # queue as un-escalated on the first deploy — the sweep would not pick it up
    # (attempts are still over the limit) and the operator queue would no longer
    # list it, which is the one state this work exists to prevent.
    op.execute("""
        UPDATE agent_actions
           SET escalated = true, escalated_at = updated_at
         WHERE verify_attempts >= 5
           AND (verification_state IN ('UNKNOWN', 'PARTIAL')
                OR (verification_state IS NULL AND status = 'PENDING'))
    """)


def downgrade() -> None:
    op.drop_index("ix_agent_actions_next_verify_at", table_name="agent_actions")
    op.drop_index("ix_agent_actions_escalated", table_name="agent_actions")
    op.drop_column("agent_actions", "next_verify_at")
    op.drop_column("agent_actions", "last_verified_at")
    op.drop_column("agent_actions", "escalated_at")
    op.drop_column("agent_actions", "escalated")
    op.drop_index("ix_mapping_lookup", table_name="provider_mappings")
    op.drop_index("ix_provider_mappings_status", table_name="provider_mappings")
    op.drop_index("ix_provider_mappings_payment_id", table_name="provider_mappings")
    op.drop_index("ix_provider_mappings_merchant_id", table_name="provider_mappings")
    op.drop_table("provider_mappings")

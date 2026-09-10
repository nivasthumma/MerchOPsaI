"""Financial controls and provenance — the architecture remediation (ADR-0053).

One migration for the columns the remediation needs, each additive:

  webhook_events       attempts, claimed_at; status widened to 16 characters
                       so it can hold FAILED (the dead letter). Processing moved
                       off the request path: persist, ACK, and a worker claims.
  agent_tasks          ai_mode, configuration_version
  approvals            policy_version, policy_input_hash, policy_decision,
                       policy_rule -- the snapshot a human approved against
  audit_logs           actor_type, actor
  event_outbox         category (DOMAIN | INTEGRATION | UI | NOTIFICATION)
  idempotency_records  new: unified application idempotency, merchant-scoped
                       under row-level security like every table with a merchant

The row-level policy for `idempotency_records` is written out here rather than
imported, for the reason `f692a958917d` gives: a migration that imports live
code stops being a snapshot of the schema at a point in time.

LOCKING: every ADD COLUMN is nullable or has a constant default, which
PostgreSQL applies as a catalogue change without rewriting the table. Widening a
varchar is likewise catalogue-only. CREATE TABLE and the policy DDL touch only
the new table.
"""
import sqlalchemy as sa
from alembic import op

revision = "b7e2c41d9a53"
down_revision = "4e1f0a7c33b8"
branch_labels = None
depends_on = None

POLICY = "merchant_isolation"
TABLE = "idempotency_records"
UNRESTRICTED = "coalesce(current_setting('app.merchant_id', true), '') = ''"
PREDICATE = (f"{UNRESTRICTED} OR merchant_id = "
             f"current_setting('app.merchant_id', true)")


def upgrade() -> None:
    # --- webhooks: asynchronous processing ---------------------------------
    op.alter_column("webhook_events", "status", type_=sa.String(length=16))
    op.add_column("webhook_events", sa.Column(
        "attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("webhook_events", sa.Column(
        "claimed_at", sa.DateTime(timezone=True), nullable=True))

    # --- agent run provenance ----------------------------------------------
    op.add_column("agent_tasks", sa.Column(
        "configuration_version", sa.String(length=32), nullable=True))
    op.add_column("agent_tasks", sa.Column("ai_mode", sa.String(length=32), nullable=True))

    # --- the approval's policy snapshot ------------------------------------
    op.add_column("approvals", sa.Column("policy_version", sa.String(length=32), nullable=True))
    op.add_column("approvals", sa.Column(
        "policy_input_hash", sa.String(length=64), nullable=True))
    op.add_column("approvals", sa.Column("policy_decision", sa.String(length=32), nullable=True))
    op.add_column("approvals", sa.Column("policy_rule", sa.String(length=64), nullable=True))

    # --- who acted, by kind -------------------------------------------------
    op.add_column("audit_logs", sa.Column("actor_type", sa.String(length=16), nullable=True))
    op.add_column("audit_logs", sa.Column("actor", sa.String(length=64), nullable=True))
    op.create_index(op.f("ix_audit_logs_actor_type"), "audit_logs", ["actor_type"])

    # --- event taxonomy -------------------------------------------------------
    op.add_column("event_outbox", sa.Column("category", sa.String(length=16), nullable=True))
    op.create_index(op.f("ix_event_outbox_category"), "event_outbox", ["category"])

    # --- unified idempotency -------------------------------------------------
    op.create_table(
        TABLE,
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=True),
        sa.Column("merchant_id", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("business_key", sa.String(length=128), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("external_reference", sa.String(length=128), nullable=True),
        sa.Column("resource_type", sa.String(length=32), nullable=True),
        sa.Column("resource_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("merchant_id", "operation", "business_key",
                            name="uq_idempotency_operation_key"),
    )
    op.create_index(op.f("ix_idempotency_records_tenant_id"), TABLE, ["tenant_id"])
    op.create_index(op.f("ix_idempotency_records_merchant_id"), TABLE, ["merchant_id"])

    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    # FORCE: the application role owns the table, and an owner is otherwise
    # exempt from its own policies.
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.execute(f"CREATE POLICY {POLICY} ON {TABLE} "
               f"FOR ALL USING ({PREDICATE}) WITH CHECK ({PREDICATE})")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.drop_index(op.f("ix_idempotency_records_merchant_id"), table_name=TABLE)
    op.drop_index(op.f("ix_idempotency_records_tenant_id"), table_name=TABLE)
    op.drop_table(TABLE)

    op.drop_index(op.f("ix_event_outbox_category"), table_name="event_outbox")
    op.drop_column("event_outbox", "category")

    op.drop_index(op.f("ix_audit_logs_actor_type"), table_name="audit_logs")
    op.drop_column("audit_logs", "actor")
    op.drop_column("audit_logs", "actor_type")

    for col in ("policy_rule", "policy_decision", "policy_input_hash", "policy_version"):
        op.drop_column("approvals", col)

    op.drop_column("agent_tasks", "ai_mode")
    op.drop_column("agent_tasks", "configuration_version")

    op.drop_column("webhook_events", "claimed_at")
    op.drop_column("webhook_events", "attempts")
    # Narrowing back is safe only once no row holds a status longer than the
    # old width. FAILED is six characters, so every value still fits.
    op.alter_column("webhook_events", "status", type_=sa.String(length=9))

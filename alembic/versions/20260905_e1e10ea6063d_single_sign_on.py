"""single sign on

OIDC authorization code flow (ADR-0050). One identity provider per tenant, and a
row per sign-in attempt so that what the browser comes back with can be checked
against what was sent.

`sso_flows` is in the database rather than a cookie or a process for two
reasons: the callback may land on a different replica than the redirect, and a
signed cookie cannot be marked consumed. This can, which is what makes a
replayed callback detectable.

`identity_providers.client_secret` is a credential in a plaintext column. That
is a known gap, scheduled rather than overlooked: column-level encryption is
Phase 3 of the readiness review. Row-level security bounds who can read it to
the owning tenant (ADR-0046) and the API never returns it.

LOCKING: two CREATE TABLEs, nothing existing is touched.

Revision ID:
Revision ID: e1e10ea6063d
Revises: 24a5de901159
Created: 2026-09-05 03:11:47.450127
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'e1e10ea6063d'
down_revision = '24a5de901159'
branch_labels = None
depends_on = None




def upgrade() -> None:
    op.create_table(
        "identity_providers",
        sa.Column("id", sa.String(64), primary_key=True),
        # Uniqueness comes from the index below, not from a separate
        # constraint: `unique=True, index=True` on the model renders as ONE
        # unique index, and declaring both here made the drift guard report a
        # constraint to remove and an index to re-add.
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"),
                  nullable=False),
        sa.Column("issuer", sa.String(500), nullable=False),
        sa.Column("client_id", sa.String(300), nullable=False),
        sa.Column("client_secret", sa.String(500), nullable=False),
        sa.Column("email_domains", sa.JSON(), nullable=False),
        sa.Column("default_role", sa.String(64), nullable=False,
                  server_default="analyst"),
        sa.Column("default_merchant_id", sa.String(64),
                  sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_index("ix_identity_providers_tenant_id", "identity_providers",
                    ["tenant_id"], unique=True)

    op.create_table(
        "sso_flows",
        sa.Column("state", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"),
                  nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("code_verifier", sa.String(128), nullable=False),
        sa.Column("redirect_to", sa.String(500), nullable=False,
                  server_default="/"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handoff_code", sa.String(64), nullable=True, unique=True),
        sa.Column("user_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_sso_flows_tenant_id", "sso_flows", ["tenant_id"])
    op.create_index("ix_sso_flows_expires_at", "sso_flows", ["expires_at"])

    # The boundary applies to both (ADR-0046). Tenant-scoped: a provider belongs
    # to the tenant that configured it, and a sign-in attempt to the tenant it
    # is signing into.
    unrestricted = "coalesce(current_setting('app.tenant_id', true), '') = ''"
    for table in ("identity_providers", "sso_flows"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY merchant_isolation ON {table}
            FOR ALL USING ({unrestricted} OR tenant_id =
                           current_setting('app.tenant_id', true))
            WITH CHECK ({unrestricted} OR tenant_id =
                        current_setting('app.tenant_id', true))""")


def downgrade() -> None:
    for table in ("sso_flows", "identity_providers"):
        op.execute(f"DROP POLICY IF EXISTS merchant_isolation ON {table}")
    op.drop_index("ix_sso_flows_expires_at", table_name="sso_flows")
    op.drop_index("ix_sso_flows_tenant_id", table_name="sso_flows")
    op.drop_table("sso_flows")
    op.drop_index("ix_identity_providers_tenant_id", table_name="identity_providers")
    op.drop_table("identity_providers")

"""scim provisioning

SCIM 2.0 (ADR-0051). ADR-0050 let a customer's employees sign IN through their
identity provider; this lets the provider tell us when one of them LEAVES.

`scim_tokens.token_hash` stores SHA-256, never the token. A provisioning
credential can create accounts, and one sitting in a readable column is one that
leaves in a database dump. It is shown once at creation.

`users.external_id` is SCIM's `externalId` -- the provider's own id for a
person. Matching on email alone breaks the day somebody's surname changes, which
for a provisioning integration means a duplicate account rather than a rename.
Nullable: users created through the API or SSO have none.

LOCKING: one CREATE TABLE and one nullable ADD COLUMN, both metadata-only.

Revision ID:
Revision ID: 075d7e974f92
Revises: e1e10ea6063d
Created: 2026-09-05 03:24:55.812638
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '075d7e974f92'
down_revision = 'e1e10ea6063d'
branch_labels = None
depends_on = None




def upgrade() -> None:
    op.create_table(
        "scim_tokens",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"),
                  nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False, server_default=""),
        sa.Column("default_merchant_id", sa.String(64),
                  sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("default_role", sa.String(64), nullable=False,
                  server_default="analyst"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", sa.String(64), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_scim_tokens_tenant_id", "scim_tokens", ["tenant_id"])
    # Unique AND indexed in one, matching  on the
    # model -- declaring a separate constraint is what the drift guard caught in
    # ADR-0050's migration.
    op.create_index("ix_scim_tokens_token_hash", "scim_tokens", ["token_hash"],
                    unique=True)

    op.add_column("users", sa.Column("external_id", sa.String(200), nullable=True))
    op.create_index("ix_users_external_id", "users", ["external_id"])

    unrestricted = "coalesce(current_setting('app.tenant_id', true), '') = ''"
    op.execute("ALTER TABLE scim_tokens ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE scim_tokens FORCE ROW LEVEL SECURITY")
    op.execute(f"""CREATE POLICY merchant_isolation ON scim_tokens
        FOR ALL USING ({unrestricted} OR tenant_id =
                       current_setting('app.tenant_id', true))
        WITH CHECK ({unrestricted} OR tenant_id =
                    current_setting('app.tenant_id', true))""")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS merchant_isolation ON scim_tokens")
    op.drop_index("ix_users_external_id", table_name="users")
    op.drop_column("users", "external_id")
    op.drop_index("ix_scim_tokens_token_hash", table_name="scim_tokens")
    op.drop_index("ix_scim_tokens_tenant_id", table_name="scim_tokens")
    op.drop_table("scim_tokens")

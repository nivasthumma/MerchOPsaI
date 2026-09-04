"""token lifecycle

Tokens that expire, rotate and can be taken away (ADR-0049).

`revoked_tokens` is a denylist by `jti` -- one session signed out without
touching the rest. `expires_at` is the token's OWN expiry and the pruning key:
once a token has expired the signature check refuses it without consulting this
table, so the row costs storage and buys nothing.

`users.credentials_valid_from` is the other shape of revocation: sign out
everywhere, as a timestamp rather than a row per live session. A self-contained
token means the server keeps no list of what it has issued, so there is nothing
to walk -- but there is always a moment to compare against.

NULL for every existing user, deliberately. A non-null value here invalidates
every token issued before it, and back-dating one would sign the whole estate
out on deploy.

LOCKING: one CREATE TABLE and one nullable ADD COLUMN. Metadata-only.

Revision ID:
Revision ID: 24a5de901159
Revises: 26e4c80167cd
Created: 2026-09-05 02:56:05.942694
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '24a5de901159'
down_revision = '26e4c80167cd'
branch_labels = None
depends_on = None




def upgrade() -> None:
    op.create_table(
        "revoked_tokens",
        sa.Column("jti", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_revoked_tokens_user_id", "revoked_tokens", ["user_id"])
    op.create_index("ix_revoked_tokens_expires_at", "revoked_tokens", ["expires_at"])
    op.add_column("users", sa.Column(
        "credentials_valid_from", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "credentials_valid_from")
    op.drop_index("ix_revoked_tokens_expires_at", table_name="revoked_tokens")
    op.drop_index("ix_revoked_tokens_user_id", table_name="revoked_tokens")
    op.drop_table("revoked_tokens")

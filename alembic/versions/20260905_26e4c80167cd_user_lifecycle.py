"""user lifecycle

Joiners, movers and leavers (ADR-0048). A user can be created, moved between
roles and offboarded through the API instead of an engineer with database
access -- which is the process an auditor spends most of a SOC 2 audit on.

DISABLED rather than deleted. `audit_logs.user_id` and
`approval_signatures.user_id` point at this row, and a trail that disappears
when somebody leaves the company is not a trail. `status` is indexed because
every authentication now filters on it.

`created_at` is added with a server default of now(), which back-dates every
existing user to the moment of migration. That is wrong and it is the best
available: the row never recorded when it was made, and inventing an earlier
date would be worse than an obviously-uniform one.

LOCKING: four ADD COLUMNs, all with constant or null defaults, metadata-only on
PostgreSQL 11+. The index on `status` is built without CONCURRENTLY and takes a
brief ACCESS EXCLUSIVE lock; `users` is small by construction.

Revision ID:
Revision ID: 26e4c80167cd
Revises: cfe752563141
Created: 2026-09-05 02:44:49.792753
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '26e4c80167cd'
down_revision = 'cfe752563141'
branch_labels = None
depends_on = None




def upgrade() -> None:
    # `sa.Enum(..., native_enum=False)`, not `sa.String(16)`. The model declares
    # an Enum, which on this dialect is a VARCHAR *plus a CHECK constraint* --
    # and the schema drift guard compares the two and caught the difference on
    # the first run. A plain String would have left the column accepting any
    # value the application happened to write.
    op.add_column("users", sa.Column(
        "status", sa.Enum("ACTIVE", "DISABLED", name="userstatus", native_enum=False),
        nullable=False, server_default="ACTIVE"))
    op.add_column("users", sa.Column(
        "deactivated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("created_by", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("deactivated_by", sa.String(64), nullable=True))
    op.add_column("users", sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False,
        server_default=sa.text("now()")))
    op.create_index("ix_users_status", "users", ["status"])


def downgrade() -> None:
    op.drop_index("ix_users_status", table_name="users")
    for column in ("created_at", "deactivated_by", "created_by",
                   "deactivated_at", "status"):
        op.drop_column("users", column)

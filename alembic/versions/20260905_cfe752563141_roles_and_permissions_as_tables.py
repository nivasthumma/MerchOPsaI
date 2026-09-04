"""roles and permissions as tables

Permissions were a JSON list on `users` (MerchantOps §66 asked for tables and
the coverage audit recorded the gap). That works, and it cannot answer "who can
approve a CRITICAL refund?" without reading every row and parsing every list,
cannot be versioned, cannot be defined per tenant, and cannot produce the
access-review evidence an auditor asks for quarterly.

THE BACKFILL IS THE RISKY PART, so it is explicit rather than clever. Every
existing (tenant_id, role) pair becomes a role row; the permission set for that
role is the UNION of what its users held. A union rather than an intersection
because losing a permission somebody currently has is a silent privilege
removal, and this migration must not change what anybody can do.

If two users share a role name within a tenant and hold DIFFERENT permissions,
the union gives both the larger set -- a privilege GRANT, which is the opposite
failure. The migration refuses to run in that case rather than choosing for you:
it is a data question about real people, and a schema change is the wrong place
to answer it silently.

LOCKING: creates three tables, adds one nullable column, backfills, then makes
the column NOT NULL and drops two. The NOT NULL takes a brief ACCESS EXCLUSIVE
lock and scans the table to validate; `users` is small by construction.

Revision ID: cfe752563141
Revision ID: cfe752563141
Revises: f692a958917d
Created: 2026-09-05 02:21:09.740107
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'cfe752563141'
down_revision = 'f692a958917d'
branch_labels = None
depends_on = None




def upgrade() -> None:
    op.create_table(
        "permissions",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )
    op.create_table(
        "roles",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("tenant_id", "name", name="uq_role_name_per_tenant"),
    )
    op.create_index("ix_roles_tenant_id", "roles", ["tenant_id"])
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.String(64),
                  sa.ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("permission_name", sa.String(64),
                  sa.ForeignKey("permissions.name"), primary_key=True),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
    )

    conn = op.get_bind()

    # --- refuse rather than guess -----------------------------------------
    conflicts = conn.execute(sa.text("""
        SELECT tenant_id, role, count(DISTINCT permissions::text) AS variants
        FROM users GROUP BY tenant_id, role HAVING count(DISTINCT permissions::text) > 1
    """)).mappings().all()
    if conflicts:
        detail = ", ".join(f"{c['tenant_id']}/{c['role']} ({c['variants']} variants)"
                           for c in conflicts)
        raise RuntimeError(
            "Cannot collapse users.permissions into roles: these (tenant, role) "
            f"pairs have users holding different permission sets -- {detail}. "
            "Collapsing them would GRANT somebody a permission they do not have "
            "today. Reconcile the users first, or split the role."
        )

    # --- the catalogue, from what is actually held -------------------------
    conn.execute(sa.text("""
        INSERT INTO permissions (name, description)
        SELECT DISTINCT p, 'Backfilled from users.permissions'
        FROM users, jsonb_array_elements_text(permissions::jsonb) AS p
        ON CONFLICT (name) DO NOTHING
    """))

    # --- one role per (tenant, role name) ----------------------------------
    conn.execute(sa.text("""
        INSERT INTO roles (id, tenant_id, name, description)
        SELECT 'ROLE_' || upper(substr(md5(tenant_id || ':' || role), 1, 12)),
               tenant_id, role, 'Backfilled from users.role'
        FROM (SELECT DISTINCT tenant_id, role FROM users) d
        ON CONFLICT (tenant_id, name) DO NOTHING
    """))

    # UNION of what that role's users held. Checked above to be a single
    # variant, so the union is exactly that variant.
    conn.execute(sa.text("""
        INSERT INTO role_permissions (role_id, permission_name)
        SELECT DISTINCT r.id, p
        FROM users u
        JOIN roles r ON r.tenant_id = u.tenant_id AND r.name = u.role
        CROSS JOIN LATERAL jsonb_array_elements_text(u.permissions::jsonb) AS p
        ON CONFLICT DO NOTHING
    """))

    # --- point users at their role, then drop the old columns --------------
    op.add_column("users", sa.Column("role_id", sa.String(64), nullable=True))
    conn.execute(sa.text("""
        UPDATE users u SET role_id = r.id
        FROM roles r WHERE r.tenant_id = u.tenant_id AND r.name = u.role
    """))
    orphans = conn.execute(sa.text(
        "SELECT count(*) FROM users WHERE role_id IS NULL")).scalar()
    if orphans:
        raise RuntimeError(
            f"{orphans} user(s) ended the backfill with no role. Refusing to "
            f"make role_id NOT NULL and lose them.")

    op.alter_column("users", "role_id", nullable=False)
    op.create_foreign_key("fk_users_role", "users", "roles", ["role_id"], ["id"])
    op.create_index("ix_users_role_id", "users", ["role_id"])
    op.drop_column("users", "permissions")
    op.drop_column("users", "role")

    # --- the boundary applies to the new tables too (ADR-0046) -------------
    for table, predicate in (
        ("roles", "tenant_id = current_setting('app.tenant_id', true)"),
        ("role_permissions",
         "EXISTS (SELECT 1 FROM roles p WHERE p.id = role_permissions.role_id)"),
    ):
        unrestricted = "coalesce(current_setting('app.tenant_id', true), '') = ''"
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY merchant_isolation ON {table}
                       FOR ALL USING ({unrestricted} OR {predicate})
                       WITH CHECK ({unrestricted} OR {predicate})""")
    # `permissions` is a catalogue of names with no tenant. Deliberately not
    # scoped: see app.tenancy.UNSCOPED_TABLES.


def downgrade() -> None:
    op.add_column("users", sa.Column("role", sa.String(64), nullable=True))
    op.add_column("users", sa.Column("permissions", sa.JSON(), nullable=True))
    conn = op.get_bind()
    conn.execute(sa.text("""
        UPDATE users u SET role = r.name,
               permissions = coalesce((
                   SELECT json_agg(rp.permission_name ORDER BY rp.permission_name)
                   FROM role_permissions rp WHERE rp.role_id = r.id), '[]'::json)
        FROM roles r WHERE r.id = u.role_id
    """))
    op.alter_column("users", "role", nullable=False)
    op.alter_column("users", "permissions", nullable=False)
    op.drop_index("ix_users_role_id", table_name="users")
    op.drop_constraint("fk_users_role", "users", type_="foreignkey")
    op.drop_column("users", "role_id")
    op.drop_table("role_permissions")
    op.drop_index("ix_roles_tenant_id", table_name="roles")
    op.drop_table("roles")
    op.drop_table("permissions")

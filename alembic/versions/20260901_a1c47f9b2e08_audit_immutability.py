"""Audit immutability — the append-only control, applied by migration.

Revision ID: a1c47f9b2e08
Revises: dfdcbe8c6ce5
Created: 2026-09-01

The triggers that make `audit_logs` append-only lived in `scripts/harden_db.py`
and were applied by whoever remembered to run `make harden`. `seed_data.
reset_schema()` calls it, so a freshly seeded database was protected — but a
database created any other way was not, and nothing said which kind you had.

That is the wrong shape for this particular control. The audit trail's whole
claim is that it cannot be rewritten; a claim that depends on a step someone
might skip is not a control, it is a convention with good intentions. Schema and
the rules that protect it belong in the same versioned, ordered, applied-once
place.

`scripts/harden_db.py` stays, and stays idempotent: it is still how you verify
the control on a database, and `harden_db.verify()` proves the trigger actually
fires rather than assuming the DDL took.
"""
from __future__ import annotations

from alembic import op

revision = "a1c47f9b2e08"
down_revision = "dfdcbe8c6ce5"
branch_labels = None
depends_on = None


TRIGGER_FN = """
CREATE OR REPLACE FUNCTION merchantops_audit_immutable()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'audit_logs is append-only: % is not permitted on this table', TG_OP
        USING ERRCODE = 'insufficient_privilege';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.execute(TRIGGER_FN)
    # Bound to the table, so they apply to every role including the owner. A
    # grant alone would not: a superuser bypasses grants and not triggers.
    op.execute("DROP TRIGGER IF EXISTS audit_no_update ON audit_logs;")
    op.execute("""CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_logs
                  FOR EACH ROW EXECUTE FUNCTION merchantops_audit_immutable();""")
    op.execute("DROP TRIGGER IF EXISTS audit_no_delete ON audit_logs;")
    op.execute("""CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_logs
                  FOR EACH ROW EXECUTE FUNCTION merchantops_audit_immutable();""")
    # Defence in depth. Best-effort because the role may already lack them or
    # may be the owner, and neither case is a failed migration.
    for stmt in ("REVOKE UPDATE ON audit_logs FROM PUBLIC;",
                 "REVOKE DELETE ON audit_logs FROM PUBLIC;"):
        try:
            op.execute(stmt)
        except Exception:                                     # noqa: BLE001
            pass


def downgrade() -> None:
    """Refused.

    Reversing this migration makes the audit trail editable. There is no
    operational reason to want that, and an automated path to it is exactly the
    thing an auditor is asking about when they ask who can rewrite history.
    """
    raise NotImplementedError(
        "Refusing to make audit_logs mutable. Removing the append-only control "
        "is a deliberate act and does not belong behind `alembic downgrade`.")

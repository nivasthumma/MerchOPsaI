"""Enforce audit immutability in the database — closes a threat-model residual risk.

The audit trail was "append-only by application convention". Convention is not a
control: any code path, migration or console session could rewrite history, and
an audit log that can be edited is not evidence.

This installs a rule PostgreSQL enforces regardless of what the application does:

    audit_logs   UPDATE -> rejected
                 DELETE -> rejected
                 INSERT -> allowed

Two layers, deliberately:

  1. BEFORE UPDATE/DELETE triggers that RAISE. These bind to the table itself, so
     they apply to every role including the table owner and superusers.
  2. REVOKE UPDATE, DELETE from the application role. Defence in depth; a
     superuser can bypass grants, but not the trigger.

Idempotent — safe to run repeatedly.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from app.db import session_scope

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

STATEMENTS = [
    TRIGGER_FN,
    "DROP TRIGGER IF EXISTS audit_no_update ON audit_logs;",
    """CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_logs
       FOR EACH ROW EXECUTE FUNCTION merchantops_audit_immutable();""",
    "DROP TRIGGER IF EXISTS audit_no_delete ON audit_logs;",
    """CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_logs
       FOR EACH ROW EXECUTE FUNCTION merchantops_audit_immutable();""",
]


def harden(session) -> None:
    for stmt in STATEMENTS:
        session.execute(text(stmt))
    # Grants are best-effort: the role may already lack them, or may be the
    # owner. Best-effort is not the same as unobserved, though — these are
    # REVOKEs on the audit log, and one failing silently is a security control
    # that did not apply and said nothing. The trigger is the real defence and
    # `verify()` below proves it fires, so a failure here is a warning rather
    # than an error; it still has to be legible.
    for stmt in ("REVOKE UPDATE ON audit_logs FROM PUBLIC;",
                 "REVOKE DELETE ON audit_logs FROM PUBLIC;"):
        sp = session.begin_nested()
        try:
            session.execute(text(stmt))
            sp.commit()
        except Exception as exc:
            sp.rollback()
            print(f"  warn  {stmt.strip()} did not apply: "
                  f"{type(exc).__name__}: {str(exc).splitlines()[0]}")


def verify(session) -> list[tuple[str, bool, str]]:
    """Prove the control works rather than assuming the DDL took effect.

    The probe row is created here rather than borrowed from existing data: a
    freshly seeded database has no audit rows, and a check that silently skips
    when there is nothing to test is worse than no check at all.
    """
    results: list[tuple[str, bool, str]] = []
    outer = session.begin_nested()
    try:
        # 1. Appends must still work, or we have broken the audit trail rather
        #    than protected it. created_at comes from the server default.
        try:
            session.execute(text("""
                INSERT INTO audit_logs (task_id, merchant_id, event_type, payload)
                VALUES ('HARDEN_PROBE', 'HARDEN', 'harden_probe', '{}'::json)
            """))
            session.flush()
            results.append(("INSERT", True, "permitted"))
        except Exception as e:
            results.append(("INSERT", False, f"BROKEN — appends rejected: {e}"))
            return results

        probe_id = session.execute(text(
            "SELECT id FROM audit_logs WHERE task_id = 'HARDEN_PROBE'"
            " ORDER BY id DESC LIMIT 1")).scalar()

        # 2. Mutation must be refused.
        for op, sql in (
            ("UPDATE", "UPDATE audit_logs SET event_type='tampered' WHERE id=:i"),
            ("DELETE", "DELETE FROM audit_logs WHERE id=:i"),
        ):
            sp = session.begin_nested()
            try:
                session.execute(text(sql), {"i": probe_id})
                sp.rollback()
                results.append((op, False, "SUCCEEDED — the audit trail is mutable"))
            except Exception as e:
                sp.rollback()
                results.append((op, True, f"rejected: {str(e).splitlines()[0][:88]}"))
    finally:
        # Discard the probe row. The DELETE trigger blocks a delete, so rolling
        # back the savepoint is the only way to remove it.
        outer.rollback()
    return results


def main() -> int:
    with session_scope() as s:
        harden(s)
    print("Applied audit immutability constraints.\n")

    with session_scope() as s:
        results = verify(s)

    ok = True
    for op, passed, detail in results:
        print(f"  {op:<8} {'OK  ' if passed else 'FAIL'}  {detail}")
        ok = ok and passed

    print()
    if ok:
        print("audit_logs is append-only, enforced by PostgreSQL.")
        return 0
    print("Audit immutability is NOT enforced.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

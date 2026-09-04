"""one live refund per payment

Closes the double-refund race. The policy engine refuses a second refund
while one is live, but that check is a SELECT with nothing holding the gap
before the INSERT: two approvals for the same payment, decided in the same
instant, both read an empty result and both proceed. `idempotency_key` does
not catch it, because the key is derived partly from `approval_id` — two
approvals produce two distinct keys and two accepted rows.

The predicate here matches the policy rule exactly (the duplicate-action
check in `app/policy/engine.py`), so the constraint and the rule cannot
drift into disagreeing about what "already refunding" means.

Locking: this is a plain CREATE UNIQUE INDEX, which holds a SHARE lock and
blocks writes to `agent_actions` for the duration. On the table sizes this
system carries that is milliseconds. It is deliberately not CONCURRENTLY —
that cannot run inside a transaction, and ADR-0030 keeps every migration
transactional so a failure leaves nothing half-applied. Zero-downtime
rollouts are out of scope (README, "designed, not built").

Revision ID: ef2c7d9613d9
Revises: 0f0125d98b5a
Created: 2026-09-04 01:44:28.175083
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = 'ef2c7d9613d9'
# Re-pointed from a1c47f9b2e08 when feat/incident-spine merged into
# feat/merchantops-v2. Both branches added migrations off the same parent,
# so the merge produced two heads and `alembic upgrade head` refused to
# run. Linearised rather than joined with a merge revision because no
# environment has either chain applied -- Vercel deployment is disabled
# and master is 38 commits behind -- and because `money_is_bigint` below
# has to run after every table exists, which a diamond does not guarantee.
down_revision = '0f0125d98b5a'
branch_labels = None
depends_on = None

_LIVE = "action_type = 'refund' AND status IN ('PENDING', 'SUBMITTED', 'CONFIRMED')"


def upgrade() -> None:
    # A database that already contains the thing this index forbids cannot be
    # indexed, and PostgreSQL's own error names the index rather than the two
    # refunds — which is the one fact an operator needs. Ask first, and fail
    # with the payments and actions involved.
    conn = op.get_bind()
    offenders = conn.execute(sa.text(f"""
        SELECT merchant_id, target_payment_id,
               count(*) AS live,
               string_agg(id, ', ' ORDER BY created_at) AS action_ids
        FROM agent_actions
        WHERE {_LIVE}
        GROUP BY merchant_id, target_payment_id
        HAVING count(*) > 1
        ORDER BY count(*) DESC
    """)).mappings().all()

    if offenders:
        lines = "\n".join(
            f"    {r['merchant_id']} / {r['target_payment_id']}: "
            f"{r['live']} live refunds — {r['action_ids']}"
            for r in offenders
        )
        raise RuntimeError(
            "Refusing to add uq_live_refund_per_payment: this database already "
            f"holds {len(offenders)} payment(s) with more than one live refund "
            "action.\n\n"
            f"{lines}\n\n"
            "These are exactly the double-refunds the constraint exists to "
            "prevent, so they predate it and need a decision rather than a "
            "default. For each one, verify against the provider which refunds "
            "actually settled, then resolve the surplus rows to FAILED (never "
            "delete them — agent_actions is the record of what was attempted). "
            "Re-run this migration afterwards."
        )

    # IF NOT EXISTS, because this migration has two arrival paths and only one
    # of them starts without the index.
    #
    # `scripts/seed_data.py` builds the schema with `create_all`, which reads
    # `app/models.py` and therefore already includes this index. That database
    # is then stamped at BASELINE and upgraded, replaying every migration after
    # it -- so the index it already has gets created a second time. CI does
    # exactly this on every run (see the "Migrations bring an existing database
    # to head" step), and a plain CREATE would fail there while succeeding on a
    # genuinely migrated database.
    #
    # This is the first post-baseline migration to add a schema object rather
    # than a trigger, which is why the collision surfaces here and not on
    # a1c47f9b2e08.
    op.execute(sa.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_live_refund_per_payment "
        f"ON agent_actions (merchant_id, target_payment_id) WHERE {_LIVE}"
    ))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS uq_live_refund_per_payment"))

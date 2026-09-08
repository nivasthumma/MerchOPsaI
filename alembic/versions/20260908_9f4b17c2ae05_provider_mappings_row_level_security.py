"""Row-level security for provider_mappings.

`provider_mappings` arrived from `feat/incident-spine`, which had no row-level
security; the policies in f692a958917d were written on `integration/trunk`,
which did not have the table. Each branch was self-consistent and the merge of
the two left exactly one table carrying a `merchant_id` and no policy.

That is not a cosmetic gap. A mapping is what ties one merchant's synthetic
payment to a real Razorpay payment id, so a query that forgets its
`WHERE merchant_id` could resolve another merchant's mapping -- and an action
placed against that id is a refund on somebody else's money. The second wall
exists for the case where the first one is missing, which is this one.

Applied in its own migration rather than folded into c3f18a4d7b62: that
migration has already run on development databases, and editing it would leave
them with the table and without the policy.

LOCKING: ALTER TABLE ... ENABLE/FORCE ROW LEVEL SECURITY and CREATE POLICY take
ACCESS EXCLUSIVE on one small table, held for the statement only.
"""
from alembic import op

revision = '9f4b17c2ae05'
down_revision = 'c3f18a4d7b62'
branch_labels = None
depends_on = None

POLICY = "merchant_isolation"
TABLE = "provider_mappings"
UNRESTRICTED = "coalesce(current_setting('app.merchant_id', true), '') = ''"
PREDICATE = (f"{UNRESTRICTED} OR merchant_id = "
             f"current_setting('app.merchant_id', true)")


def upgrade() -> None:
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    # FORCE, because the application role owns the table and PostgreSQL exempts
    # an owner from its own policies without it -- a control that reports as
    # present and filters nothing.
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    # FOR ALL with WITH CHECK, so a row cannot be written INTO another merchant
    # either, not merely read out of one.
    op.execute(f"CREATE POLICY {POLICY} ON {TABLE} "
               f"FOR ALL USING ({PREDICATE}) WITH CHECK ({PREDICATE})")


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS {POLICY} ON {TABLE}")
    op.execute(f"ALTER TABLE {TABLE} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} DISABLE ROW LEVEL SECURITY")

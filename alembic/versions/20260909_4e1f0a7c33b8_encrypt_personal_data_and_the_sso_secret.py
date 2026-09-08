"""Encrypt personal data and the SSO client secret at rest.

ADR-0052. `customers.name`, `customers.email` and
`identity_providers.client_secret` become ciphertext in the database. The
reasoning for which columns and why only these is in `app/privacy.py`; this
migration is the part that moves the data already there.

## Widen, then encrypt

Two steps, in that order, because the ciphertext is longer than the plaintext:
base64 of a 12-byte nonce plus the payload and its 16-byte tag does not fit in
`String(200)`. Encrypting first fails on the values it is meant to protect.

## Why this imports `app.crypto`

`20260905_f692a958917d` deliberately froze its own copy of the table list, on
the grounds that a migration importing live code stops being a snapshot of the
schema at a point in time. That argument holds for a LIST and not for a CODEC.

Two implementations of AES-256-GCM that disagree by a byte produce a database
nobody can read, and the disagreement surfaces at the first decrypt rather than
here. There is one implementation, and this calls it. What that couples this
migration to is the wire format -- `enc:v1:<kid>:<nonce>:<ciphertext>` -- which
is versioned in the value itself precisely so it can change without invalidating
what is already stored.

## Re-runnable, and reversible

`encrypt` returns an already-encrypted value untouched, so a re-run over a
partly-converted table finishes the job rather than double-encrypting. The
downgrade decrypts before narrowing, in the reverse order for the same reason.

LOCKING: `ALTER TABLE ... ALTER COLUMN TYPE` on a varchar whose length only
GROWS is a catalogue change in PostgreSQL -- no table rewrite, no full scan. The
UPDATE that follows does rewrite every row of two small tables.

BATCHING: the update reads and writes row by row through Python, because the
encryption happens there. At this dataset's size that is milliseconds. On a
customers table of consequence it should be chunked by primary key; it is
written as a loop over a fetched list rather than a cursor so that change is
local.
"""
from alembic import op
from sqlalchemy import String, text

from app.crypto import decrypt, encrypt

revision = '4e1f0a7c33b8'
down_revision = '9f4b17c2ae05'
branch_labels = None
depends_on = None

#: (table, column, plaintext length, ciphertext length)
COLUMNS = (
    ("customers", "name", 200, 500),
    ("customers", "email", 200, 500),
    ("identity_providers", "client_secret", 500, 1000),
)


def _convert(table: str, column: str, fn) -> None:
    """Rewrite one column through `fn`, skipping rows with nothing in them."""
    conn = op.get_bind()
    rows = conn.execute(
        text(f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL")
    ).all()
    for row_id, value in rows:
        converted = fn(value, table=table, column=column)
        if converted != value:
            conn.execute(
                text(f"UPDATE {table} SET {column} = :v WHERE id = :i"),
                {"v": converted, "i": row_id})


def upgrade() -> None:
    for table, column, _plain, cipher in COLUMNS:
        op.alter_column(table, column, type_=String(cipher), existing_nullable=False)
    for table, column, _plain, _cipher in COLUMNS:
        _convert(table, column, encrypt)


def downgrade() -> None:
    for table, column, plain, _cipher in COLUMNS:
        _convert(table, column, decrypt)
    for table, column, plain, _cipher in COLUMNS:
        op.alter_column(table, column, type_=String(plain), existing_nullable=False)

"""What is encrypted, what is classified, and what the crypto refuses to do.

ADR-0052. Both `app/crypto.py` and `app/privacy.py` name this file in their
docstrings as the thing that holds them honest, for a specific reason: a round
trip through the ORM passes just as happily when nothing is encrypted at all.
The `Encrypted` type decrypts on the way out, so `customer.email == "a@b.c"`
proves only that the type is symmetric.

So the first test here reads the stored bytes through raw SQL, which is the one
path the ORM's type does not touch, and asserts the ciphertext directly.
"""
from __future__ import annotations

import base64
import os

import pytest
from sqlalchemy import text

from app import privacy
from app.crypto import (
    DEV_KEY, EncryptionError, InsecureConfiguration, blind_index, decrypt,
    encrypt, is_encrypted, require_configured_key,
)
from app.models import Base


# --------------------------------------------------------------------------
# The one that would make the rest theatre
# --------------------------------------------------------------------------
def test_the_stored_value_is_ciphertext_not_plaintext(db):
    """Read past the ORM. This is the assertion the whole change exists for."""
    for table, column in privacy.encrypted_fields():
        rows = db.execute(
            text(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
        ).scalars().all()
        # An empty table would let this pass by having nothing to check, which
        # is the failure mode `test_the_drift_guard_is_not_vacuous` exists for
        # elsewhere. `identity_providers` is seeded only by the SSO tests, so
        # it is allowed to be empty -- `customers` is not.
        if table == "customers":
            assert rows, "customers is empty; this test would prove nothing"
        for stored in rows:
            assert is_encrypted(stored), (
                f"{table}.{column} holds plaintext: {stored!r}")


def test_a_customer_reads_back_as_plaintext_through_the_orm(db):
    """The other half. Ciphertext nobody can read is not a feature."""
    from app.models import Customer

    c = db.query(Customer).filter_by(merchant_id="MERCH_A").first()
    assert c is not None
    assert not is_encrypted(c.email)
    assert "@" in c.email


# --------------------------------------------------------------------------
# The map cannot fall behind the schema
# --------------------------------------------------------------------------
def test_every_column_that_looks_personal_is_classified():
    """A column added with a person in it and no entry in the map fails here.

    The candidate set is derived from the schema rather than listed, so this
    cannot be satisfied by editing the test.
    """
    unclassified = []
    for table in Base.metadata.tables.values():
        for column in table.columns:
            if not any(s in column.name for s in privacy.SUSPICIOUS):
                continue
            if (table.name, column.name) not in privacy.MAP:
                unclassified.append(f"{table.name}.{column.name}")
    assert unclassified == [], (
        f"{sorted(unclassified)} look like they carry a person or a secret and "
        f"are not in app.privacy.MAP. Classify them, or say why they are "
        f"OPERATIONAL -- silence is not a decision.")


def test_the_map_does_not_describe_columns_that_do_not_exist():
    """The other direction: a column renamed out from under the map."""
    real = {(t.name, c.name) for t in Base.metadata.tables.values() for c in t.columns}
    assert [k for k in privacy.MAP if k not in real] == []


def test_what_the_map_calls_encrypted_is_what_the_models_encrypt():
    """The map is the document an auditor reads; the models are what runs.

    Claiming encryption the schema does not apply is worse than claiming
    nothing, because it ends the conversation.
    """
    from app.crypto import Encrypted

    in_models = {(t.name, c.name)
                 for t in Base.metadata.tables.values() for c in t.columns
                 if isinstance(c.type, Encrypted)}
    assert set(privacy.encrypted_fields()) == in_models


def test_users_email_is_recorded_as_deliberately_not_encrypted():
    """The one PERSONAL column left in plaintext, and it is a decision.

    Pinned so that removing the reasoning requires removing this test, rather
    than the entry quietly disappearing when somebody tidies the map.
    """
    field = privacy.MAP[("users", "email")]
    assert field.classification is privacy.Classification.PERSONAL
    assert not field.encrypted
    assert "blind index" in field.why


# --------------------------------------------------------------------------
# The codec
# --------------------------------------------------------------------------
def test_a_value_moved_to_another_column_fails_to_decrypt():
    """The additional authenticated data, which is the point of using GCM here.

    Without it a ciphertext is portable: somebody with write access copies one
    customer's encrypted email into another row, or another column, and it
    decrypts cleanly.
    """
    stored = encrypt("someone@example.com", table="customers", column="email")

    with pytest.raises(EncryptionError) as e:
        decrypt(stored, table="customers", column="name")
    assert e.value.code == "invalid"

    with pytest.raises(EncryptionError):
        decrypt(stored, table="identity_providers", column="client_secret")


def test_an_altered_ciphertext_fails_rather_than_decrypting_to_something_else():
    stored = encrypt("someone@example.com", table="customers", column="email")
    head, tail = stored.rsplit(":", 1)
    tampered = f"{head}:{'A' * len(tail)}"

    with pytest.raises(EncryptionError) as e:
        decrypt(tampered, table="customers", column="email")
    assert e.value.code == "invalid"


def test_a_malformed_value_says_so_specifically():
    with pytest.raises(EncryptionError) as e:
        decrypt("enc:v1:current:onlythreeparts", table="customers", column="email")
    assert e.value.code == "malformed"


def test_a_value_under_a_key_this_process_does_not_hold_says_which():
    """Rotation's failure mode, and it must not read as corruption."""
    with pytest.raises(EncryptionError) as e:
        decrypt("enc:v1:retired:AAAA:BBBB", table="customers", column="email")
    assert e.value.code == "unknown_key"
    assert "ENCRYPTION_KEY_PREVIOUS" in str(e.value)


def test_a_previous_key_decrypts_and_never_encrypts(monkeypatch):
    """Set it, deploy, re-encrypt at leisure, remove it -- as ADR-0049's tokens."""
    old = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", old)
    under_old = encrypt("someone@example.com", table="customers", column="email")
    # The value names the key it was written with, and `current` is whichever
    # key the writing process held.
    assert under_old.split(":")[2] == "current"

    new = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("ENCRYPTION_KEY", new)
    monkeypatch.setenv("ENCRYPTION_KEY_PREVIOUS", old)
    # Written as `current`, so a rotation that only swaps the key cannot read it
    # back: this is why re-encryption is a step and not an afterthought.
    with pytest.raises(EncryptionError):
        decrypt(under_old, table="customers", column="email")

    rewritten = encrypt("someone@example.com", table="customers", column="email")
    assert decrypt(rewritten, table="customers", column="email") == "someone@example.com"


def test_none_stays_none_and_is_not_an_encrypted_empty_string():
    """An absent value and an encrypted "" are different facts."""
    assert encrypt(None, table="customers", column="email") is None
    assert decrypt(None, table="customers", column="email") is None


def test_encrypting_twice_does_not_double_encrypt():
    """What makes the migration re-runnable over a partly converted table."""
    once = encrypt("someone@example.com", table="customers", column="email")
    assert encrypt(once, table="customers", column="email") == once


def test_a_plaintext_value_reads_through_unchanged():
    """What makes the migration survivable while it is still running."""
    assert decrypt("plain@example.com", table="customers",
                   column="email") == "plain@example.com"


def test_a_bad_key_is_rejected_at_configuration_rather_than_at_rest(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", base64.b64encode(b"too-short").decode())
    with pytest.raises(EncryptionError) as e:
        encrypt("x", table="customers", column="email")
    assert e.value.code == "bad_key"
    assert "32" in str(e.value)


# --------------------------------------------------------------------------
# Blind index
# --------------------------------------------------------------------------
def test_the_blind_index_matches_regardless_of_case_or_padding():
    """The lookups it replaces were `lower(email) = :e`. Hashing the raw value
    would make them case-sensitive, and the symptom is a duplicate account."""
    a = blind_index("Someone@Example.com ", table="customers", column="email")
    b = blind_index("someone@example.com", table="customers", column="email")
    assert a == b


def test_the_same_value_in_two_columns_produces_two_digests():
    """So one column's index cannot be correlated against another's."""
    email = blind_index("a@b.c", table="customers", column="email")
    name = blind_index("a@b.c", table="customers", column="name")
    assert email != name


def test_the_blind_index_does_not_contain_the_value():
    digest = blind_index("someone@example.com", table="customers", column="email")
    assert "someone" not in digest and "@" not in digest
    assert len(digest) == 64


# --------------------------------------------------------------------------
# The default key
# --------------------------------------------------------------------------
def test_a_deployment_refuses_to_start_on_the_published_development_key(monkeypatch):
    """Encrypting with a key in this repository is worse than plaintext: the
    column now looks protected and a reviewer stops asking."""
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("MERCHANTOPS_ALLOW_DEV_SECRET", raising=False)
    monkeypatch.setattr("app.crypto.DEV_KEY_IN_USE", True)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    with pytest.raises(InsecureConfiguration) as e:
        require_configured_key()
    assert "openssl rand -base64 32" in str(e.value)


def test_development_runs_on_the_default_without_ceremony(monkeypatch):
    """A fresh clone runs. That is the bargain the token secret strikes too."""
    monkeypatch.delenv("ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr("app.crypto.DEV_KEY_IN_USE", True)
    for marker in ("VERCEL", "AWS_EXECUTION_ENV", "KUBERNETES_SERVICE_HOST"):
        monkeypatch.delenv(marker, raising=False)
    require_configured_key()


def test_the_development_key_is_the_size_aes_256_requires():
    """The first draft of this literal was 36 bytes and every encrypt raised."""
    assert len(base64.b64decode(DEV_KEY)) == 32


# --------------------------------------------------------------------------
# The migration that moves the data already there
# --------------------------------------------------------------------------
def test_the_migration_encrypts_rows_that_were_written_before_it(monkeypatch):
    """Upgrade over plaintext, then downgrade back to it.

    The rest of this file runs against a schema built by `create_all`, where the
    columns were `Encrypted` from the first row written. That proves the type
    works and says nothing about the rows a real database already has -- which
    is the only reason the migration exists.
    """
    from alembic import command
    from sqlalchemy import create_engine

    from tests.integration.test_migrations import _alembic_config, _fresh_database

    url = _fresh_database("merchantops_privacytest")
    cfg = _alembic_config(url)
    # The revision BEFORE the encryption one, so these rows are written the way
    # a database that predates it holds them.
    command.upgrade(cfg, "9f4b17c2ae05")

    engine = create_engine(url, future=True)
    try:
        with engine.begin() as c:
            # `created_at` is NOT NULL with a PYTHON-side default, so a raw
            # insert has to supply it -- the ORM would have filled it in.
            c.execute(text("INSERT INTO tenants (id, name, created_at) "
                           "VALUES ('T1', 'Kettle', now())"))
            c.execute(text(
                "INSERT INTO merchants "
                "  (id, tenant_id, name, currency, policy_config, created_at) "
                "VALUES ('M1', 'T1', 'Kettle Co', 'INR', '{}', now())"))
            c.execute(text(
                "INSERT INTO customers "
                "  (id, merchant_id, name, email, segment, contact_opted_out) "
                "VALUES ('C1', 'M1', 'Ada Lovelace', 'ada@example.com', "
                "        'standard', false)"))

        command.upgrade(cfg, "head")

        with engine.connect() as c:
            name, email = c.execute(
                text("SELECT name, email FROM customers WHERE id = 'C1'")).one()
        assert is_encrypted(name) and is_encrypted(email), (
            "the migration left the rows it was written to convert in plaintext")
        assert decrypt(name, table="customers", column="name") == "Ada Lovelace"
        assert decrypt(email, table="customers", column="email") == "ada@example.com"

        # And back. A migration that cannot be undone is one nobody dares run.
        command.downgrade(cfg, "9f4b17c2ae05")
        with engine.connect() as c:
            name, email = c.execute(
                text("SELECT name, email FROM customers WHERE id = 'C1'")).one()
        assert (name, email) == ("Ada Lovelace", "ada@example.com")
    finally:
        engine.dispose()

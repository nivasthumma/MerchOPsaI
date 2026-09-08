"""Encrypting the columns that hold a person, or a credential.

`customers.name`, `customers.email` and the free text beside them are a real
person's details sitting in plaintext, and `identity_providers.client_secret` is
a credential that can impersonate this application to a customer's identity
provider. Both leave in a database dump, a backup, a replica, or a screenshot of
a query result, and none of those are reached through the API where every other
control in this system lives.

## The shape of a stored value

    enc:v1:<kid>:<nonce>:<ciphertext>

AES-256-GCM, so the ciphertext carries its own authentication tag: a value that
has been altered fails to decrypt rather than decrypting to something else.

**The additional authenticated data is `table.column`.** That is the part worth
pausing on. Without it, a ciphertext is portable -- somebody with write access
could copy one customer's encrypted email into another customer's row, or into a
different column, and it would decrypt cleanly. Binding it means a value that has
been moved fails to decrypt at all, which is the failure you want: loud, and at
the moment of the read.

## Searching what you cannot read

An encrypted column cannot be used in `WHERE email = :e`, and the application
does exactly that. A *blind index* is the standard answer: a keyed hash of the
normalised value, stored beside the ciphertext and searched instead.

It leaks equality -- two rows with the same email have the same index -- and that
is the trade being made deliberately, because the alternative is either no
encryption or no lookup. It does NOT leak the value: the key is not in the
database, so a dump gives an attacker a set of opaque digests they cannot
reverse without also holding the key.

The index key is derived from the master key rather than configured separately,
so there is one secret to rotate and one to lose.

## Rotation

`kid` travels in the value. `ENCRYPTION_KEY_PREVIOUS` decrypts but never
encrypts, exactly as the token signing key works (ADR-0049): set it, deploy,
re-encrypt at leisure, remove it. A value under a key the process no longer
holds says so specifically rather than reporting corruption.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import String
from sqlalchemy.types import TypeDecorator

PREFIX = "enc"
VERSION = "v1"

#: The development fallback, and the same bargain the token secret strikes: a
#: fresh clone runs with no configuration, and a deployment refuses to start on
#: it. Not a secret -- it is in this file -- which is the point of
#: `require_configured_key`.
#: 32 bytes exactly, because AES-256 takes nothing else. The first draft of
#: this literal was 36 bytes and every encrypt raised -- caught on the first
#: call rather than at rest, which is the right place for that mistake.
DEV_KEY = "ZGV2LW9ubHktaW5zZWN1cmUtZW5jcnlwdGlvbi1rZXk="


class EncryptionError(Exception):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class InsecureConfiguration(RuntimeError):
    """The process would encrypt with a key published in this repository."""


def _material(name: str) -> bytes | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
    except Exception as exc:
        raise EncryptionError(f"{name} is not base64.", "bad_key") from exc
    if len(key) != 32:
        raise EncryptionError(
            f"{name} decodes to {len(key)} bytes; AES-256 needs 32. "
            f"Generate one with: openssl rand -base64 32", "bad_key")
    return key


def _keys() -> dict[str, bytes]:
    keys = {"current": _material("ENCRYPTION_KEY") or base64.b64decode(DEV_KEY)}
    previous = _material("ENCRYPTION_KEY_PREVIOUS")
    if previous:
        keys["previous"] = previous
    return keys


DEV_KEY_IN_USE = os.environ.get("ENCRYPTION_KEY") is None


def require_configured_key() -> None:
    """Refuse to serve on a deployment that would encrypt with the default.

    Encrypting with a key published in this repository is not encryption. It is
    worse than plaintext, because the column now looks protected: a reviewer
    sees ciphertext and stops asking.

    Deliberately the same shape as `require_configured_secret` (ADR-0025), down
    to the escape hatch, because it is the same argument about the same class of
    default.
    """
    if not DEV_KEY_IN_USE or os.environ.get("MERCHANTOPS_ALLOW_DEV_SECRET"):
        return

    from app.api.security import deployment_context

    marker = deployment_context()
    if marker is None:
        return
    raise InsecureConfiguration(
        f"Refusing to start: ENCRYPTION_KEY is unset, so personal data and the "
        f"SSO client secret would be encrypted with the development default "
        f"published in this repository. {marker} says this is a deployment.\n\n"
        f"Anyone who can read the repository could decrypt the database.\n\n"
        f"    ENCRYPTION_KEY=$(openssl rand -base64 32)\n\n"
        f"Or set MERCHANTOPS_ALLOW_DEV_SECRET=1 to accept the risk explicitly."
    )


# --------------------------------------------------------------------------
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(f"{PREFIX}:{VERSION}:")


def encrypt(value: str | None, *, table: str, column: str) -> str | None:
    """Encrypt one value, bound to the column it belongs in.

    None passes through as None: an absent value and an encrypted empty string
    are different facts, and collapsing them would make a nullable column
    un-nullable in practice.
    """
    if value is None:
        return None
    if is_encrypted(value):
        return value

    nonce = os.urandom(12)
    ciphertext = AESGCM(_keys()["current"]).encrypt(
        nonce, value.encode(), f"{table}.{column}".encode())
    return f"{PREFIX}:{VERSION}:current:{_b64(nonce)}:{_b64(ciphertext)}"


def decrypt(stored: str | None, *, table: str, column: str) -> str | None:
    """Read one value back.

    A value that is not encrypted is returned as it is. That is what makes the
    migration survivable: rows written before the change, and rows written by a
    replica that has not deployed it yet, still read correctly. It also means
    this cannot detect a column somebody forgot to encrypt -- which is why
    `tests/integration/test_privacy.py` asserts the stored form directly.
    """
    if stored is None or not is_encrypted(stored):
        return stored

    try:
        _, _, kid, nonce, ciphertext = stored.split(":", 4)
    except ValueError as exc:
        raise EncryptionError("Encrypted value is malformed.", "malformed") from exc

    key = _keys().get(kid)
    if key is None:
        raise EncryptionError(
            f"This value was encrypted under key {kid!r}, which this process "
            f"does not hold. Set ENCRYPTION_KEY_PREVIOUS if you are mid-rotation.",
            "unknown_key")

    try:
        return AESGCM(key).decrypt(
            _unb64(nonce), _unb64(ciphertext), f"{table}.{column}".encode()).decode()
    except Exception as exc:
        # An InvalidTag here means the ciphertext was altered, truncated, or
        # copied from a different column. All three are the same answer.
        raise EncryptionError(
            f"Could not decrypt {table}.{column}: the value has been altered or "
            f"moved from another column.", "invalid") from exc


# --------------------------------------------------------------------------
# Blind index
# --------------------------------------------------------------------------
def blind_index(value: str | None, *, table: str, column: str) -> str | None:
    """A keyed digest for equality lookups on an encrypted column.

    Normalised (stripped, lower-cased) before hashing, because the lookups this
    replaces were already case-insensitive -- `lower(email) = :e`. Hashing the
    raw value would silently make those lookups case-sensitive, and the symptom
    would be a duplicate account rather than an error.

    The key is derived from the master key by HMAC over a fixed label, so
    rotating the master rotates this too -- and re-indexing is therefore part of
    re-encrypting rather than a separate thing to remember. The column is in the
    derivation, so the same address in two columns produces two different
    digests and one cannot be correlated with the other.
    """
    if value is None:
        return None
    key = hmac.new(_keys()["current"], b"blind-index-v1", hashlib.sha256).digest()
    return hmac.new(key, f"{table}.{column}:{value.strip().lower()}".encode(),
                    hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# The ORM path
# --------------------------------------------------------------------------
class Encrypted(TypeDecorator):
    """A column that is ciphertext at rest and plaintext in Python.

    Applied to the model so the ORM path cannot forget. It does NOT cover raw
    SQL -- `session.execute(text("SELECT email FROM customers"))` returns the
    stored ciphertext, because SQLAlchemy has no type information there. That is
    a real edge and the reason `tests/integration/test_privacy.py` asserts the
    stored form directly rather than trusting a round trip through the ORM: a
    round trip would pass just as happily if nothing were encrypted at all.
    """
    impl = String
    cache_ok = True

    def __init__(self, length: int, *, table: str, column: str):
        # The column's identity is baked in at class definition, which is what
        # binds the ciphertext to its home (see the module docstring). It is not
        # derivable at runtime -- SQLAlchemy does not tell a type where it is.
        super().__init__(length)
        self._table = table
        self._column = column

    def process_bind_param(self, value, dialect):
        return encrypt(value, table=self._table, column=self._column)

    def process_result_value(self, value, dialect):
        return decrypt(value, table=self._table, column=self._column)

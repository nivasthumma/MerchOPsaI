"""What personal data this system holds, where, and for how long.

The map an auditor asks for first, and the artefact every other privacy control
is derived from: you cannot encrypt, retain, export or erase what nobody has
written down.

It is code rather than a document because a document goes stale silently. A
column added with a person's details in it and no entry here fails
`tests/integration/test_privacy.py`, which is the same "silence is not a
decision" rule `app.tenancy.UNSCOPED_TABLES` follows.

## The classes, and why these four

`PERSONAL`      identifies a living person. DPDP and GDPR both attach to it.
`CREDENTIAL`    not personal, and more dangerous: it grants access.
`MERCHANT_TEXT` free text a merchant wrote, which MAY contain personal data and
                usually does not. Treated as personal for erasure and retention,
                and not encrypted -- the agent reads it, the detection rules
                search it, and encrypting it would break both to protect a
                maybe.
`OPERATIONAL`   no person in it. Listed anyway where the name looks like it
                might be, so that "we checked" is recorded rather than assumed.

## Why some PERSONAL columns are not encrypted

`users.email` is the identifier three subsystems match on -- lifecycle, SSO and
SCIM all find an account by address. Encrypting it needs the blind index wired
through each of those, and doing that in the same change as introducing the
crypto would mean shipping two risks together. It is named here as the next
step rather than left to be noticed.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class Classification(str, enum.Enum):
    PERSONAL = "PERSONAL"
    CREDENTIAL = "CREDENTIAL"
    MERCHANT_TEXT = "MERCHANT_TEXT"
    OPERATIONAL = "OPERATIONAL"


@dataclass(frozen=True)
class Field:
    classification: Classification
    why: str
    #: Encrypted at rest, with a blind index where it is searched.
    encrypted: bool = False
    #: True when erasing a data subject must clear or overwrite this.
    erasable: bool = False


P, C, M, O = (Classification.PERSONAL, Classification.CREDENTIAL,
              Classification.MERCHANT_TEXT, Classification.OPERATIONAL)

#: Every column whose NAME suggests it might carry a person or a secret. The
#: test derives that candidate set from the schema and asserts each one appears
#: here, so this cannot fall behind the models.
MAP: dict[tuple[str, str], Field] = {
    # --- the actual people -------------------------------------------------
    ("customers", "name"): Field(P, "A customer's name", encrypted=True, erasable=True),
    ("customers", "email"): Field(P, "A customer's address; used to contact them",
                                  encrypted=True, erasable=True),
    ("customers", "notes"): Field(M, "Merchant free text about a customer. Read by "
                                     "the agent and searched by detection, so not "
                                     "encrypted; erased with the customer",
                                  erasable=True),
    ("users", "email"): Field(P, "An operator's address. NOT encrypted yet: it is "
                                 "the identifier lifecycle, SSO and SCIM all match "
                                 "on, and wiring the blind index through those is "
                                 "the next step"),

    # --- credentials -------------------------------------------------------
    ("identity_providers", "client_secret"): Field(
        C, "Impersonates this application to a customer's identity provider",
        encrypted=True),
    ("scim_tokens", "token_hash"): Field(
        C, "Already a SHA-256 digest, so there is nothing to encrypt: the "
           "credential itself was never stored"),

    # --- merchant free text, which may contain anything --------------------
    ("orders", "notes"): Field(M, "Merchant free text on an order", erasable=True),
    ("payments", "notes"): Field(M, "Merchant free text on a payment", erasable=True),
    ("incidents", "title"): Field(M, "Generated from a detection rule, not typed"),
    ("approvals", "action_payload"): Field(
        M, "The arguments of a proposed action. Carries payment ids, not people"),

    # --- audit and events: personal data can reach these -------------------
    ("audit_logs", "payload"): Field(
        M, "Redacted on write, and append-only by trigger. NOT erasable: the "
           "trail is the compliance record, and DPDP's erasure right does not "
           "extend to records held to satisfy another legal obligation"),
    # It was NOT pruned by the drain, which only marks `published_at`. The
    # table grew without bound from the day it shipped, and nothing noticed
    # because this line said otherwise. `app/retention.py` prunes it now.
    ("event_outbox", "payload"): Field(M, "Redacted on write; pruned once "
                                          "published (app/retention.py)"),
    ("event_outbox", "payload_hash"): Field(O, "A digest of the payload"),
    ("webhook_events", "payload"): Field(
        M, "The provider's own body. Carries payment identifiers"),
    ("webhook_events", "payload_hash"): Field(O, "A digest"),

    # --- notifications: an operator's address and what was said to them ----
    ("operator_notifications", "recipient"): Field(
        P, "The operator's address a notification went to", erasable=True),
    ("operator_notifications", "recipient_user_id"): Field(
        O, "A user id, despite the column name"),
    ("operator_notifications", "title"): Field(M, "Rendered message subject"),
    ("operator_notifications", "body"): Field(
        M, "Rendered message body; redacted on write", erasable=True),

    # --- looks like PII, is not. Listed so the check is recorded -----------
    ("identity_providers", "email_domains"): Field(
        O, "Domains, not addresses: `kettle.example`, never a person"),
    ("merchants", "name"): Field(O, "A company"),
    ("tenants", "name"): Field(O, "A company"),
    ("products", "name"): Field(O, "A product"),
    ("permissions", "name"): Field(O, "A permission, e.g. action:refund"),
    ("roles", "name"): Field(O, "A role, e.g. analyst"),
    ("role_permissions", "permission_name"): Field(O, "A permission name"),
    ("scim_tokens", "name"): Field(O, "A label for a provisioning token"),
    ("tool_calls", "tool_name"): Field(O, "A tool, e.g. get_payment"),
}


def encrypted_fields() -> tuple[tuple[str, str], ...]:
    return tuple(k for k, v in MAP.items() if v.encrypted)


def erasable_fields() -> tuple[tuple[str, str], ...]:
    return tuple(k for k, v in MAP.items() if v.erasable)


def personal_fields() -> tuple[tuple[str, str], ...]:
    return tuple(k for k, v in MAP.items()
                 if v.classification in (Classification.PERSONAL,
                                         Classification.MERCHANT_TEXT))


#: Column names that make a column a candidate for classification. Used by the
#: test, not by the application: its job is to notice a new column that looks
#: like it holds a person, so the list is deliberately over-eager.
SUSPICIOUS = ("email", "name", "notes", "secret", "token", "password",
              "phone", "address", "recipient", "body", "title", "payload")

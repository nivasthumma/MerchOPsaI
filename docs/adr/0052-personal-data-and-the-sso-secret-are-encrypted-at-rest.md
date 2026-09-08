# ADR-0052 — Personal data and the SSO secret are encrypted at rest

**Status:** Accepted · 2026-09-09
**Phase 3 of the readiness review, item one.**

## Context

Every control in this system guards the API. Authentication (ADR-0049), the
permission catalogue (ADR-0047), row-level security (`f692a958917d`) — all of
them decide who may call what. None of them applies to a database dump, a
backup, a read replica, or a screenshot of a query result.

Two kinds of value in this schema are worth something to somebody holding one
of those:

- `customers.name` and `customers.email` — a real person, and the point at
  which DPDP and GDPR attach.
- `identity_providers.client_secret` — a credential that impersonates this
  application to a customer's identity provider. When SSO shipped, the model
  carried a comment naming it as scheduled debt: *"a credential sitting in a
  column, and Phase 3 is where column-level encryption arrives."*

## Decision

Encrypt those three columns at rest with AES-256-GCM, stored as

```
enc:v1:<kid>:<nonce>:<ciphertext>
```

**The additional authenticated data is `table.column`.** Without it a
ciphertext is portable: somebody with write access copies one customer's
encrypted email into another customer's row, or into a different column, and it
decrypts cleanly. Binding it means a moved value fails to decrypt at all —
loudly, at the moment of the read.

**Applied as a SQLAlchemy type**, so the ORM path cannot forget. It does not
cover raw SQL, which is a real edge rather than an oversight: `text("SELECT
email FROM customers")` returns what is stored, because SQLAlchemy has no type
information there. Four such reads existed and now decrypt at the edge — the
console's search, the payment lifecycle, and the agent's `get_order` and
`get_customer`. That the ORM round trip is not the proof is why
`tests/integration/test_privacy.py` asserts the **stored** form through raw
SQL: a round trip passes just as happily when nothing is encrypted at all.

**Rotation follows ADR-0049 exactly**, because it is the same problem. `kid`
travels in the value; `ENCRYPTION_KEY_PREVIOUS` decrypts and never encrypts.
Set it, deploy, re-encrypt at leisure, remove it. A value under a key the
process no longer holds says so specifically rather than reporting corruption.

**A deployment refuses to start on the development key**, the same bargain
`require_configured_secret` strikes (ADR-0025). Encrypting with a key published
in this repository is worse than plaintext: the column now looks protected and
a reviewer stops asking.

## What is deliberately NOT encrypted

`app/privacy.py` is the map, and it is code rather than a document because a
document goes stale silently — a column added with a person in it and no entry
there fails a test.

- **`users.email`.** The identifier lifecycle, SSO and SCIM all match an
  account on. Encrypting it needs the blind index wired through three
  subsystems, and doing that in the same change as introducing the crypto ships
  two risks together. Named in the map as the next step.
- **Merchant free text** (`customers.notes`, `orders.notes`, `payments.notes`).
  The agent reads it and detection searches it. Encrypting it would break both
  to protect a maybe. Classified `MERCHANT_TEXT`: treated as personal for
  erasure and retention, and not encrypted.
- **`audit_logs.payload`.** Redacted on write and append-only by trigger. Not
  erasable either: the trail is the compliance record.

## The blind index, and why none is needed yet

An encrypted column cannot be used in `WHERE email = :e`. Nothing searches
`customers` by name or address — every read is by `id` — so no index is needed
for these three columns today. `app/crypto.blind_index` exists and is tested
because `users.email` will need it, and building it alongside the crypto it
derives its key from is cheaper than retrofitting it.

It leaks equality: two rows with the same address produce the same digest. That
is the trade, made deliberately, because the alternative is either no
encryption or no lookup. It does not leak the value — the key is not in the
database.

## Consequences

- A dump, a backup or a replica no longer carries readable personal data or a
  usable SSO secret.
- Raw SQL that reads an encrypted column returns ciphertext. Four call sites
  handle it; a fifth added later will read `enc:v1:…` and look broken, which is
  the failure mode to expect.
- The ciphertext is longer than the plaintext, so the columns widened —
  `String(200)` to `String(500)`, and the client secret to `String(1000)`. The
  first migration written for this failed on exactly that.
- Losing `ENCRYPTION_KEY` loses the data. That is what encryption at rest
  means, and it moves key custody onto the deployment checklist.
- `cryptography` becomes a direct dependency rather than a transitive one.

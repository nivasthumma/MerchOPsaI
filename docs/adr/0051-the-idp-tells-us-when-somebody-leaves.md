# ADR-0051 — The identity provider tells us when somebody leaves

**Status:** Accepted · 2026-09-05
**Phase 2, follow-on. Closes the gap ADR-0050 named.**

## Context

ADR-0050 let a customer's employees sign in through their identity provider. It
did not let the provider tell us when one of them *leaves*.

So an employee removed at Okta kept working here until an owner remembered to
disable them — and "we deprovision through our IdP" is a sentence a customer
says in a security review that would not have been true. ADR-0050's own
"what this does not do" section named it as the next thing an auditor would
find.

## Decision

SCIM 2.0 — RFC 7643 for the schema, RFC 7644 for the protocol. `/scim/v2/Users`
with list-and-filter, create, read, replace, patch and delete, plus the three
discovery documents a provider reads before it will talk to you at all.

**Deprovisioning is the operation everything else supports.** `active: false`
and `DELETE` both mean DISABLED, and both revoke every token the person holds
(ADR-0049). A deprovisioning that leaves a live session is deprovisioning in
name only.

**Never a hard delete.** `audit_logs.user_id` and `approval_signatures.user_id`
point at the row (ADR-0048). RFC 7644 §3.6 permits disabling instead of
removing, and a subsequent GET returning 404 is what makes it look like a delete
to the client.

**A separate, long-lived credential.** A SCIM client is a machine configured
once: Okta and Entra hold a static bearer token and have nowhere to put a
refresh flow. ADR-0049's tokens expire, which is right for a person and wrong
for this. `scim_tokens` stores **SHA-256, never the token** — a credential that
can create accounts is one that must not leave in a database dump — and the
value is shown once.

**The tenant comes from the token, and then binds.** `tenancy.bind` runs on the
SCIM dependency exactly as it does on a user's, so row-level security bounds
every read and write (ADR-0046) independently of the queries getting it right.

### Shapes providers actually send

Entra deactivates with `{"op": "replace", "value": {"active": false}}` — no
`path`. Okta sends `{"op": "replace", "path": "active", "value": false}`.
Handling only the pathed form would mean deprovisioning silently doing nothing
for every Entra customer, so both are handled and both are tested.

An operation on an attribute this application does not model — a phone number,
a department — is ignored rather than rejected. A provider syncing a field we do
not keep must not have its `active` operation refused because of it.

`userName eq "…"` is the filter every provider sends to decide create-or-update.
An unsupported filter is refused with `scimType: invalidFilter` rather than
ignored: ignoring it would hand a provider the whole directory and let it
conclude that every user matches.

## Consequences

**A provisioning integration may not create owners,** refused at token creation.
Same rule as SSO, and for the same reason: an IdP deciding who administers the
tenant means anybody who can create an account there can administer this one.

**The last active owner cannot be deprovisioned.** The same guard ADR-0048 put
on the API, reached through a different door — and here it matters more, because
the IdP has no idea it has just left a tenant nobody can administer.

**`last_used_at` answers "is the integration actually running?"** which is the
question asked when somebody's offboarding did not take effect, and which
nothing could answer before.

**Two exceptions to house rules, both deliberate.**

Request models ignore unknown attributes. Every response model in this
application forbids extra keys, so a field the server sends and does not declare
fails loudly. SCIM requests are the other way round: Okta sends `name`,
`phoneNumbers`, `meta` and enterprise-extension attributes, and rejecting a
payload for containing what the standard says a client may send would mean SCIM
does not work.

SCIM responses are exempt from the forbid-extra-keys guard, listed explicitly in
`RFC_DEFINED_ROUTES`. That guard exists to stop the *SPA's* contract rotting;
these routes are consumed by Okta and Entra and never by the frontend, and
modelling RFC 7643 in pydantic would mean fighting an extensibility the standard
requires. `tests/integration/test_scim.py` asserts their shape directly instead.

**The contract guard gained a real fix along the way.** It required every route
to declare a response model, which is wrong for a `204 No Content`: there is no
body, so a model would describe something never sent. It now asserts the
opposite for 204s.

## What this does not do

**No Groups.** Mapping an IdP's groups onto roles is a genuinely larger design —
what happens to somebody in two groups, what happens when a group is renamed,
whether a group may grant `owner` — and both Okta and Entra provision users
without it. Advertised as unsupported in `ServiceProviderConfig` rather than
left for a provider to discover by trying.

**No `PUT` of a role.** A provisioned user gets the token's `default_role` and
changes role through the MerchantOps API. Letting the IdP set it is the same
authority question as Groups.

**No bulk, no sort, no ETags.** All advertised as `false`. A truthful `false`
saves a support ticket.

**No SAML still.** Unchanged from ADR-0050: it needs `xmlsec` and a native
build. A customer whose IdP is SAML-only can now be *deprovisioned* through
SCIM but cannot sign in through their provider at all.

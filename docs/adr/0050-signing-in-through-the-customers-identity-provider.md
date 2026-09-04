# ADR-0050 — Signing in through the customer's identity provider

**Status:** Accepted · 2026-09-05
**Phase 2 of the readiness review, item five. Completes Phase 2.**

## Context

Every enterprise security review asks for SSO before it asks anything else, and
until now the answer was a bearer token an engineer minted by hand. ADR-0048 put
user creation behind an API and ADR-0049 gave tokens a lifecycle, but the
credential was still something this system issued rather than something the
customer's identity provider vouched for.

## Decision

**OIDC authorization code flow with PKCE.** One identity provider per tenant,
routed by email domain — the only fact a sign-in box has before anybody is
authenticated.

    /auth/sso/start     ->  302 to the IdP, with state, nonce, PKCE challenge
    /auth/sso/callback  <-  code + state, exchanged server-to-server
    /auth/sso/exchange  ->  a one-time handoff code becomes a token pair

**No credential ever travels in a URL.** The callback redirects with a
single-use handoff code, not a token. A token in a fragment or query lands in
browser history, in the referrer of whatever the page loads next, and in every
proxy log along the way.

**The flow lives in a table, not a cookie or a process.** The callback may land
on a different replica than the redirect, and a signed cookie cannot be marked
consumed. A row can, which is what makes a replayed callback detectable rather
than merely unlikely.

### The ID token's signature is not verified

OIDC Core §3.1.3.7 clause 6: *"If the ID Token is received via direct
communication between the Client and the Token Endpoint … the TLS server
validation MAY be used to validate the issuer in place of checking the token
signature."*

That is exactly this flow. The token is never accepted from the browser. It is
fetched by this server from the discovered `token_endpoint`, over TLS with a
validated certificate chain, authenticating with the client secret.

Verifying it anyway would mean a JWKS cache, `kid` selection and an RS256
implementation — a native cryptography dependency and a well-populated family of
JWS bugs (algorithm confusion, unverified `kid` fetching, key substitution)
bought to re-establish something TLS already has.

**This reasoning does not extend to a token arriving any other way.** In the
implicit or hybrid flows the token comes through the browser and its signature is
the only thing between an attacker and an identity. Those flows are not
implemented, and implementing one means implementing verification first. The
module docstring says so where somebody would be about to add one.

Everything TLS does *not* establish is checked: `iss` exactly, `aud` against our
client id, `exp` against the clock, `nonce` against this flow, and
`email_verified` — an unverified address is one somebody claimed, and matching an
account on it would let anyone who can add an address at the customer's IdP sign
in as somebody else.

### Provisioning, and the role an IdP may not grant

A first-time user is created with the provider's `default_role`. **Never
`owner`** — an identity provider deciding who administers a tenant means anybody
who can create an account at the customer's IdP can administer their
MerchantOps. Refused at configuration time and again at provisioning, because
the two are reachable independently.

A DISABLED user who authenticates successfully stays disabled. Their provider
still recognising them is not new information about a decision this system made.

## Consequences

**Two tenants cannot claim one email domain.** Routing would be a coin toss, and
the coin would decide which company an employee signs in to. Refused with 409.

**Discovery runs before the configuration is stored,** so a typo in the issuer
fails for the owner setting it up rather than for the first person who tries to
sign in.

**The client secret goes in and does not come out.** `GET /sso` omits it
entirely rather than masking it — a masked secret is still a shape somebody
tries to read.

**It is stored in a plaintext column, and that is a scheduled debt.** Row-level
security bounds who can read it to the owning tenant, and column-level
encryption is Phase 3. Named in the model, the migration and here, so it is a
decision with a date rather than an oversight.

**Redirect targets are validated against an allowlist shape.** An open redirect
on a login endpoint is how a phishing page borrows somebody else's domain: the
victim sees a legitimate host, signs in, and is handed to the attacker.
`//evil.example` is a protocol-relative URL that looks like a path, and is
tested.

**The drift guard caught the migration again** — `unique=True, index=True` on
the model renders as one unique index, and the migration declared a constraint
plus a plain index. Four consecutive changes now.

## What this does not do

**No SAML.** It needs XML signature verification, which means `xmlsec` and a
native build — a materially larger dependency than everything this application
currently has, for a protocol most identity providers offer OIDC alongside.
Customers who can only do SAML are a real segment and this does not serve them.

**No SCIM.** Deprovisioning still happens through `PATCH /users/{id}`, so an
employee removed at the IdP keeps working here until somebody says so. That is
the gap an auditor will find next, and it is the natural follow-on: the lifecycle
operations SCIM needs all exist now.

**No IdP-initiated sign-in**, no `prompt`/`max_age` handling, no back-channel
logout, and one provider per tenant.

**The token still lives in `localStorage`.** There is finally a session to put
in an httpOnly cookie, and that is a change to the SPA's whole auth path rather
than an addition to this one.

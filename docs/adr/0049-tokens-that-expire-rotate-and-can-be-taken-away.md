# ADR-0049 — Tokens that expire, rotate, and can be taken away

**Status:** Accepted · 2026-09-05
**Phase 2 of the readiness review, item four.**

## Context

Authentication was `HMAC-SHA256(user_id)` under a server secret. Unforgeable,
compared in constant time, with permissions re-read from the database on every
request — all correct, and the README has carried the limitation since the day
it shipped: *"no expiry, rotation, revocation list, or audience binding."*

ADR-0048 sharpened it rather than closing it. A leaver's token now dies with
their row, because `authz.resolve` filters on status. A **leaked** token still
worked until somebody changed the server secret, which signs out everybody.

Four failures, four properties:

| a token… | needs |
|---|---|
| copied off a laptop | to stop working on its own — **expiry** |
| known to be compromised | to stop working now — **revocation** |
| signed with a key being retired | to survive the change — **rotation** |
| minted for something else | to be refused here — **binding** |

## Decision

`mo1.<payload>.<signature>` — base64url JSON, HMAC-SHA256 over the exact
payload bytes. Claims: `sub`, `typ`, `jti`, `iat`, `exp`, `kid`.

**Not a JWT, deliberately.** JWT's algorithm agility is its best-known
vulnerability class — `alg: none`, RS256-to-HS256 confusion — and none of what
it buys applies here: one issuer, one verifier, one algorithm. A format with no
algorithm field cannot be confused about the algorithm. The payload is readable
with `base64 -d`, which matters more at three in the morning than
interoperability with a library nobody is using.

**`iat` and `exp` carry microseconds, not whole seconds.** JWT's convention is
integers, and it produced a real bug here: `credentials_valid_from` refuses
everything issued before a moment, and at one-second granularity a token minted
in the same second as a reset is indistinguishable from one minted just before
it. Either it survives a sign-out it should not have, or a legitimate sign-in a
moment later is refused. Both were observed. Sub-second timestamps make the
comparison exact rather than a choice about rounding.

**Rotation is `kid` plus an overlap window.** `API_TOKEN_SECRET_PREVIOUS`
verifies but never signs. Set it to the old value, deploy, and tokens already in
the wild keep working until they expire. A key id the server no longer holds
reports `unknown_key`, not `bad_signature` — "the overlap window closed" and
"somebody is forging" send whoever is paged to different places.

**Revocation has two shapes, because there are two questions.**

*This token*, by `jti`, in `revoked_tokens`. Signing out of one browser.

*Every token for this user*, by `users.credentials_valid_from`. A timestamp, not
a sweep: a self-contained token means the server keeps no list of what it has
issued, so there is nothing to walk — but there is always a moment to compare
against. Signing out everywhere costs one update rather than one insert per live
session.

**Legacy tokens are refused by default.** `AUTH_ACCEPT_LEGACY_TOKENS` exists for
the length of a rollout. Accepting a format with no expiry indefinitely would
make every property above optional for anybody still holding an old one, and
`/health` reports the setting for the same reason it reports the development
secret.

## Refresh, and the replay

A refresh token is single-use. Presenting one returns a new pair and revokes the
one presented. **Presenting an already-used refresh token signs the account out
of everything** — not just that token.

That is blunt and it is the right blunt. A second presentation means two parties
hold the same token: theft, or a client bug. The server cannot tell which holder
is legitimate, and guessing wrong leaves an attacker with a session. Everybody
signing in again is the cheaper error.

The legitimate half of the exchange dies too. It was minted moments before the
replay was detected and is as compromised as the token that was replayed.

## Consequences

**A security response was being rolled back by the act of reporting it.** The
first version revoked every session on replay and then raised, the route turned
the exception into a 401, the 401 left `session_scope`, and `session_scope`
rolled back — so every session stayed live while the log said they had been
closed. `app.db.checkpoint` now commits the revocation first: the same primitive
ADR-0029 introduced for the action record, for the same reason. Some writes have
to outlive the failure that follows them.

**Offboarding revokes tokens as well as refusing the user.** ADR-0048 made a
disabled user unresolvable; this revokes what they hold. The two answer
different questions, and the day somebody adds a lookup that skips `resolve`,
the revocation is the one still standing. A consequence worth stating: re-enabling
an account no longer resurrects the old token, which is the right way round.

**The client refreshes once, then gives up.** A 401 on a request that carried a
token is ordinarily "it aged out". One refresh, one retry — a loop around an
endpoint that mints credentials is how a client turns an expired session into a
flood. Both tokens are replaced on success, because keeping the old refresh
token would guarantee the next attempt is treated as a replay.

**`issue_token` kept its name and changed its shape.** Thirty-seven call sites
use it and none of them care what a token looks like, which is the point of them
going through one function.

**The forgery test now forges the current format.** It demonstrated that the
published development secret can mint a token for any user — by minting a
*legacy* token, which is now refused outright. That would have shown the control
working while the actual format went untested.

## What this does not do

**No audience or issuer claim.** There is one audience. Adding `aud` now would
be a field nothing checks, and a check nothing exercises is a check that will be
wrong when it finally matters.

**No asymmetric signing.** Every verifier is this application. Public-key
signing earns its cost when something else has to verify without being able to
mint, and nothing does.

**The token is still a bearer credential in `localStorage`.** Any script on the
origin can read it. httpOnly cookies with CSRF protection are the answer and
they arrive with SSO, where there is a session to put in one.

**No per-device sessions.** `jti` makes one token revocable but nothing records
which device or browser it came from, so "sign out my old phone" means signing
out everywhere. A `sessions` table with a user agent and a last-seen is the
missing piece.

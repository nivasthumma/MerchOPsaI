# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability**
on this repository. It reaches the maintainer privately, so please use it rather
than opening a public issue.

Please include what you did, what happened, and what you expected — a failing
scenario id or a `curl` against a local instance is ideal.

## Scope

This is an independent demonstration project. Two things bound what a
vulnerability here can affect:

- **No real money moves.** Payment execution runs against a mock adapter unless
  Razorpay Test Mode credentials are supplied, and Test Mode itself is not real
  money. There is no production integration.
- **All data is synthetic.** The dataset is generated from seed `20260825`. No
  real customer, merchant, or payment data exists in this repository.

Findings in the control plane are nonetheless interesting and in scope — most of
the project's value is there:

| Area | Why it matters |
|---|---|
| Policy engine | Anything that yields ALLOW where the rules say DENY or REQUIRE_APPROVAL |
| Approval workflow | Executing an action without a valid, unexpired, re-checked approval |
| Merchant isolation | Reading or acting across a merchant boundary |
| Idempotency | Any path producing two refunds from one approval |
| Prompt injection | Merchant-controlled text changing a *policy outcome* — note the claim is about decisions and external calls, not about the model's prose |
| Audit integrity | Mutating `audit_logs`, or a secret surviving `redact()` into the trail |
| Authentication | Forging a token, or permissions coming from anywhere but the database |

## What is already known and documented

Please check `README.md` § Known limitations before reporting. Rate limiting is
per-worker, authentication tokens have no expiry or revocation list, and
reconciliation is a sweep rather than a daemon. These are recorded scope
decisions, not undiscovered defects.

# Runbook

For whoever is holding the pager. Everything here is a command you can run or a
query you can paste, against the system as it is actually built — not a
description of how it ought to work.

Two facts shape every procedure below:

- **This system moves money.** The dangerous failure is not an outage, it is a
  refund issued twice or a refund reported as settled that never happened.
  Several procedures therefore end in "escalate" rather than "retry", and that
  is the correct ending.
- **A provider's HTTP 200 is not proof.** Verification reads business state
  back. When an outcome is genuinely not known the system records `UNKNOWN`
  rather than guessing, and `UNKNOWN` is a state somebody has to resolve.

---

## 1. Is it healthy?

```bash
curl -s localhost:8000/health | python -m json.tool
```

Read these four fields before anything else:

| Field | What a bad value means |
|---|---|
| `llm_provider` | `deterministic` when you expected `anthropic` means no credential was found. Reasoning is a rule-based planner. |
| `payment_adapter` | `mock` when you expected `live_test_mode` means no Razorpay credential. **No external call is reaching the provider.** |
| `auth_secret_is_development_default` | `true` on a deployment should now be impossible — the app refuses to start. If you see it, something set `MERCHANTOPS_ALLOW_DEV_SECRET`. Treat every token as forgeable. |
| `signing_secret_is_development_default` | Webhook signatures are not being verified against a real secret. |

Then the objectives:

```bash
curl -s localhost:8000/metrics/objectives -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

Two of these have a target of **zero** and are not latency measures. Either one
non-zero is a page, not a ticket:

- `unauthorized_executions` — an external action that does not trace to an
  approved approval. This is the core safety property of the system.
- `unverified_success_claims` — an action marked `CONFIRMED` without an
  independent read-back saying `SUCCESS`. The system is claiming money moved on
  something it did not check.

Both are covered in §5.

---

## 2. The daily job: unresolved `UNKNOWN` actions

`UNKNOWN` means a refund was submitted and the response was lost. The money may
or may not have moved. A pending state nobody resolves is not safety, it is
deferral, so there is a sweep.

```bash
make reconcile          # or: .venv/bin/python scripts/reconcile.py
```

Exit codes, which a cron wrapper should act on:

| Code | Meaning | Action |
|---|---|---|
| `0` | Nothing outstanding, or everything settled | None |
| `2` | One or more actions were **escalated** — the sweep gave up | Work the queue below |

**The sweep never retries the action.** It re-reads provider state by the
action's own idempotency key. A blind retry of a financial action with an
unknown outcome is the most dangerous thing this system could do, and the sweep
cannot perform one. Do not "help" it by re-approving.

Three properties bound it: actions younger than 30 seconds are skipped (a refund
may simply not have propagated), attempts stop at five, and settlement is a read.

### The escalation queue

```bash
curl -s localhost:8000/actions/escalated -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

These are actions reconciliation could not settle after five attempts. Each one
needs a human to look at the provider's own dashboard and decide. Resolve by
answering one question: **did the refund reach the provider?**

- **Yes, and it settled** → the action is real. Record it settled.
- **Yes, and the provider failed it** → mark `FAILED`. The payment is refundable
  again; the constraint in §6 only bounds *live* refunds.
- **No record at the provider at all** → it never left. Mark `FAILED`.

Never resolve one by issuing a second refund. If a customer is owed money and
the first attempt is genuinely gone, that is a *new* approval with a fresh
decision behind it, not a repair of the old one.

---

## 3. Deployment

```bash
make migrate            # brings any database to head; handles all three states
make migrate-status     # what would run, without running it
make migrate-sql        # the SQL, printed for review before a production change
```

`migrate.py` handles the three states a database can be in — empty,
existing-but-unstamped, already stamped — which a bare `alembic upgrade head`
does not.

**Before running a migration on a live database, read its docstring.** Two in
this repository carry operational cost that autogenerate cannot tell you about:

- `ef2c7d9613d9` (one live refund per payment) refuses to run if the database
  already contains a payment with two live refunds, and names them. That is not
  a bug in the migration — those are pre-existing double refunds and they need a
  decision, not a default.
- `d09395a87106` (money is bigint) rewrites every table with a monetary column
  and holds `ACCESS EXCLUSIVE` for the duration. Milliseconds at this system's
  volumes; not milliseconds at a real merchant's. Read the docstring before
  running it against a large `payments` table.

### Required configuration

The application **refuses to start** on a deployment without `API_TOKEN_SECRET`.
That is deliberate — the fallback key is a literal in the source, so tokens
signed with it are forgeable by anyone who can read the repository.

```bash
API_TOKEN_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
```

"A deployment" means one of `VERCEL`, `AWS_EXECUTION_ENV`,
`KUBERNETES_SERVICE_HOST`, `DYNO`, `RENDER`, `FLY_APP_NAME`,
`WEBSITE_INSTANCE_ID` is set, or `MERCHANTOPS_ENV` is `production`/`staging`.

### Rotating the signing secret

There is no key id in the token and no rotation mechanism, so rotation is a
hard cutover: **every existing token stops working at once.** Tokens carry
identity only and permissions are read from the database per request, so the
blast radius is "everyone re-authenticates", not "someone keeps stale
authority". Mint replacements with `make token USER_ID=…`.

This is a known limitation, not an oversight — see the README. Revoking one
person today means rotating for everybody.

---

## 4. Backup and restore

**Nothing in this repository automates backups.** It is a demonstration project
running on a single PostgreSQL instance with no replication. Before this system
carries anything real, that has to change. What follows is the minimum.

### What must survive

`audit_logs` is the one table you cannot reconstruct. It is append-only,
enforced by a PostgreSQL trigger, and it is the evidence that an action was
authorised. Business data can in principle be re-derived from the provider;
the record of *who approved what* cannot.

### Taking a backup

```bash
pg_dump --format=custom --file=merchantops-$(date -u +%Y%m%dT%H%M%SZ).dump merchantops
```

### Restoring, and the step people skip

```bash
createdb merchantops_restored
pg_restore --dbname=merchantops_restored merchantops-<timestamp>.dump

# The triggers are the control. Prove they came back.
DATABASE_URL=postgresql+psycopg2://…/merchantops_restored make harden
```

That last command is not optional. `pg_restore` restores the trigger *function*
and the triggers, but a restore that silently lost them leaves an audit log that
looks identical and is no longer append-only. `make harden` re-applies and then
**proves** the control by attempting an UPDATE and a DELETE and requiring both
to be refused. An audit trail nobody has verified is a claim, not evidence.

### Objectives

These are the numbers to argue about before an incident, not during one. They
are stated as what the current architecture actually supports:

| | Value | Why |
|---|---|---|
| **RPO** | = backup interval | No replication, no WAL archiving. With nightly dumps you lose up to a day. |
| **RTO** | restore time + verify | Single instance, no standby. Rehearsed on the seeded dataset (96 KB dump) in under a second; **not** timed against a production-sized one. Do that before quoting a number to anybody. |

The procedure above was rehearsed on 2026-09-04, not just written: dump,
restore into a fresh database, confirm `audit_no_update` and `audit_no_delete`
survived `pg_restore`, then `make harden` to prove both are refused. They were.
That tells you the shape of the procedure is right. It does not tell you what it
costs at scale, which is the number an RTO actually needs.

Both are worse than a payments system should accept. Closing them means
streaming replication and point-in-time recovery, which is real infrastructure
work and is not pretended to exist here.

---

## 5. Triage

### `unauthorized_executions` is not zero

The most serious signal this system produces: an external action with no
approval behind it.

```sql
SELECT a.id, a.merchant_id, a.action_type, a.target_payment_id,
       a.amount_minor, a.status, a.approval_id, a.created_at
FROM agent_actions a
LEFT JOIN approvals ap ON ap.id = a.approval_id
WHERE a.approval_id IS NULL OR ap.decision <> 'APPROVED'
ORDER BY a.created_at DESC;
```

Do not restart anything. The trail is the asset — get the correlation id and
read the whole operation before changing state:

```bash
curl -s "localhost:8000/trace/$CORRELATION_ID" -H "Authorization: Bearer $TOKEN"
```

### `unverified_success_claims` is not zero

An action is `CONFIRMED` without a verification that says `SUCCESS`. The system
is reporting money moved on something it did not read back. Re-verify rather
than trusting either value:

```bash
curl -s -X POST "localhost:8000/tasks/$TASK_ID/reverify" -H "Authorization: Bearer $TOKEN"
```

### A task failed and you want to know whether to retry

Do not guess. The taxonomy answers exactly this:

```bash
curl -s localhost:8000/failures/taxonomy -H "Authorization: Bearer $TOKEN" | python -m json.tool
```

Every failure code carries a `retryability`, an owning subsystem, and a
recommended next action. The values that matter:

| Retryability | What to do |
|---|---|
| `NEVER` | Retrying reproduces the result identically. Change the action or the policy. `POLICY_DENIED` and `AUTHORIZATION_DENIED` are here — they are decisions, not errors. |
| `BOUNDED_BACKOFF` | Retry with backoff and jitter. If it persists, escalate rather than continuing to call. |
| `ESCALATE` | A human decides. `APPROVAL_EXPIRED` is here: request a fresh decision, do not extend the old one. |

### Rate limiting is firing unexpectedly

The counter is **in-process**. With more than one worker the effective limit is
the per-worker limit times the worker count, and a client can be refused by one
worker while another is idle. This is a documented limitation — a shared counter
needs Redis. If limits look wrong, check the worker count before the code.

---

## 6. Things that are supposed to fail

Not every refusal is an incident. These are controls working:

| You see | It means |
|---|---|
| `concurrent_refund_refused` | Two approvals raced for one payment. The database allowed exactly one. Working as designed — do not re-approve. |
| `duplicate_action` | The same approval was executed twice. The second call did not reach the provider. |
| `not_externally_mapped` | The payment has no provider mapping. Only the mapped subset can execute externally. |
| A webhook stored with status `INVALID` | A signature failed. It was recorded and **not** processed. An unverified event can never change state. |
| `integer out of range` | Should no longer be possible — money columns are `bigint`. If you see it, a new column was added as `Integer`; `test_every_money_column_is_64_bit` exists to catch that. |

---

## 7. What this runbook cannot help with

Stated plainly, because a runbook that implies more coverage than exists is
worse than a short one:

- **No alerting.** Nothing pages anybody. The metrics exist and are exported;
  wiring them to a receiver is not done here.
- **No always-on reconciliation.** The sweep is cron-driven. Webhooks settle the
  common path in near real time, but an action nobody gets an event for waits
  for the next sweep.
- **No horizontal scale story.** Single process, synchronous. The in-process
  rate limiter and the `NullPool` serverless configuration both assume this.
- **No tested disaster recovery.** See §4. The procedure is written; nobody has
  rehearsed it against a production-sized dataset, and an unrehearsed restore is
  a hypothesis.

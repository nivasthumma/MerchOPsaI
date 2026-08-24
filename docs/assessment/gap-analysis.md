# Gap analysis

Contract (as amended by ADR-0008) versus what is implemented.

## Implemented and verified

| Contract § | Requirement | Evidence |
|---|---|---|
| §5, §6 | Synthetic/Test-Mode boundary + mapping layer | `payments.external_payment_id`, `resolve_external_payment()` |
| §7 | Day-0 feasibility spike with honest fallback | `scripts/razorpay_spike.py`, `docs/assessment/razorpay-spike.md` |
| §10 | Bounded agent + execution budget | `AgentRuntime`, `test_budget_terminates_runaway_loop` |
| §12, §13 | Typed registry, strict schemas, validation first | `app/tools/registry.py`, ADR-0010 |
| §14 | `ToolResult` + typed `Finding` with citations | `app/tools/contracts.py` |
| §16 | Seven seeded incident classes | `scripts/seed_data.py` |
| §19, §20 | Risk model + deterministic policy engine | `app/policy/engine.py`, 13 security tests |
| §21 | Server-side expiring approval, re-checked | `app/agent/approval.py` |
| §22 | Adapter owns provider specifics | `app/integrations/razorpay/` |
| §24 | `agent_actions` reserve-before-call + UNIQUE key | `test_double_approval_produces_one_refund` |
| §25, §26 | Read-back verification; UNKNOWN resolvable | `test_lost_response_yields_unknown_then_resolves` |
| §27, §39 | Append-only audit, secrets redacted | `app/audit/trace.py` |
| §28 | Replay: PLAYBACK + RE_REASON, divergence classified | `app/agent/replay.py`, ADR-0006 |
| §29–§32 | 103 scenarios grading observable behaviour | `data/scenarios/scenarios.yaml`, 103/103 |
| §33 | All five required security scenarios | SEC-01…SEC-05 |
| §34 | 15 classified failure codes | `app/models.py` |
| §35A | Fault-injection seam | `app/integrations/razorpay/faults.py` |
| §36 | Injection defence with a mechanism | `untrusted` tagging + delimiters |
| §37, §38 | Secret isolation, merchant isolation | 4 isolation tests, 404 not 403 |
| §40, §41 | Streamlit UI + API incl. `/reverify` | `ui/`, `app/api/main.py` |
| §42 | 13 tables incl. `agent_actions` | `app/models.py` |
| §46 | 10 ADRs | `docs/adr/` |
| §49 | Definition of done | see below |
| §58 | Both acceptance tests | `tests/integration/test_flows.py` |

## Not implemented (deliberate)

| Gap | Reason |
|---|---|
| Real Razorpay execution | No credentials; spike verdict `mock`. Disclosed in README. |
| Model-backed reasoning metrics | No API key; deterministic planner used. Disclosed. |
| Always-on reconciliation worker | Would need Redis/Celery, cut by §52. Implemented as a cron-able sweep with an escalation queue instead. |
| CI regression gate | Suite and mutation test both run locally; not yet wired into CI. |
| Next.js, Redis, Celery, containers | §52 forbids them in the MVP. |
| Real identity provider | Header-based stand-in; principal still resolved server-side. |

## Definition of done (§49)

| Criterion | Status |
|---|---|
| User can ask a revenue/payment question | ✅ |
| Agent uses controlled tools | ✅ 6 typed tools |
| Synthetic incidents reproducible | ✅ seed 20260825, verified identical across runs |
| Duplicate payments detected | ✅ computed confidence, not hardcoded |
| Policy enforced outside the model | ✅ |
| Unauthorized actions blocked | ✅ |
| High-risk requires approval | ✅ |
| Approved action executes externally | ⚠️ **via mock adapter** — documented, not claimed as real |
| Synthetic→external mapping explicit | ✅ |
| Resulting state independently verified | ✅ reads back the payment |
| UNKNOWN supported | ✅ and resolvable |
| Duplicate execution prevented | ✅ |
| Audit persisted | ✅ |
| Replay without side effects | ✅ asserted, not assumed |
| 100+ scenarios executable | ✅ 103/103, validated by 13/13 mutation testing |
| Adversarial cases tested | ✅ 23/23 |
| Actual evaluation results generated | ✅ measured, counts not percentages |
| README distinguishes built vs designed | ✅ table near the top |
| Demo completable in 5–7 minutes | ✅ `make demo` |

One criterion carries a caveat, and it is stated plainly rather than quietly passed.

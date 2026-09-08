"""How long each table keeps its rows, and which ones are never dropped.

Every table here accumulates a row per event, and nothing bounded any of them.
`revoked_tokens` and `sso_flows` were already pruned (`job_prune_tokens`); this
is the rest, and the tables deliberately left alone.

It is code rather than a document for the same reason `app/privacy.py` is: a
table added with an unbounded growth pattern and no entry here fails a test.

## What may be deleted is a narrower question than what may be forgotten

Three things make a row safe to remove, and all three have to hold:

    it has been CONSUMED    the outbox row was published, the notification sent
    nothing READS it        no code path, no operator screen, no auditor
    it is not EVIDENCE      it does not prove a decision or a movement of money

Most of this schema fails the third test. `webhook_events` is what verification
reads to establish that a provider did what it said, `agent_actions` carries
amounts, and everything hanging off `agent_tasks` is the record of why an
action was taken. A retention policy that frees disk by deleting the reason a
refund happened has not saved anything worth having.

## `audit_logs` is not on the list, and that is the point

It is append-only, enforced by a PostgreSQL trigger, and `app/privacy.py`
already records why it is not erasable: the trail is the compliance record, and
an erasure right does not extend to records held to satisfy another legal
obligation. Deleting from it would mean suspending the trigger that makes it
evidence -- a control this system goes out of its way to prove is on, including
after a restore.

So it grows, and that is a decision rather than an oversight. When it becomes a
volume problem the answer is to ARCHIVE -- copy to cold storage, verify the
copy, then remove within the same transaction that recorded the archival -- not
to add a delete path to the one table whose value is that it has none.

## The numbers are the part that is not an engineering decision

The periods below are defaults chosen to be obviously safe, not a compliance
answer. How long a payment operator must keep a delivered notification is a
question for whoever owns the retention schedule; the mechanism does not care
what the number is, and every one of them is configurable.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text


@dataclass(frozen=True)
class Policy:
    """What happens to one table's rows over time.

    `days is None` means kept indefinitely, and `why` then has to say what makes
    that the right answer rather than the unexamined one.
    """
    days: int | None
    why: str
    #: SQL that must hold for a row to be eligible, beyond being old enough.
    #: Age alone is never sufficient: a row nobody consumed is not stale, it is
    #: unfinished.
    eligible: str | None = None
    #: The column age is measured from.
    column: str | None = None


MAP: dict[str, Policy] = {
    # --- consumed, unread, and not evidence --------------------------------
    "event_outbox": Policy(
        30, "Published rows have been delivered to every consumer; the row is a "
            "delivery receipt, not the event. `app/privacy.py` claimed the drain "
            "pruned these and it never did -- the drain sets `published_at` and "
            "nothing deleted, so this table grew without bound from the day it "
            "shipped",
        eligible="published_at IS NOT NULL", column="published_at"),
    "operator_notifications": Policy(
        90, "A delivery record for a message already sent. Nothing reads it back "
            "-- the incident it concerns is the durable object",
        eligible="sent_at IS NOT NULL", column="sent_at"),
    "evaluation_results": Policy(
        90, "Scenario-run output. Platform measurement, not merchant data, and "
            "no code path reads a historical run: `data/evaluation_report.json` "
            "is what publishes a score",
        column="created_at"),

    # --- kept, each for a stated reason ------------------------------------
    "audit_logs": Policy(
        None, "The compliance record. Append-only by trigger, and not erasable "
              "(see app/privacy.py). Growth is handled by archiving, never by a "
              "delete path -- see this module's docstring"),
    "webhook_events": Policy(
        None, "What the provider told us, and what verification reads to "
              "establish that a movement of money actually happened. Deleting it "
              "makes an old reconciliation unrepeatable"),
    "agent_tasks": Policy(
        None, "The investigation record, and the parent of agent_actions, "
              "approvals, agent_messages and tool_calls. Pruning it either "
              "orphans or cascades into rows carrying amounts"),
    "agent_actions": Policy(
        None, "Carries the amount, the provider reference and the verification "
              "state of real money. This is the ledger"),
    "agent_messages": Policy(
        None, "What the model was asked and answered, which is how an operator "
              "or an auditor reconstructs why an action was proposed"),
    "tool_calls": Policy(
        None, "Which tool ran with which arguments. The grounding evidence "
              "behind every claim the agent made"),
    "approvals": Policy(
        None, "Who authorised a movement of money, and when"),
    "incidents": Policy(
        None, "The durable object everything else refers to"),
}


def prunable() -> tuple[str, ...]:
    return tuple(t for t, p in MAP.items() if p.days is not None)


def kept() -> tuple[str, ...]:
    return tuple(t for t, p in MAP.items() if p.days is None)


def prune(session, *, now: datetime | None = None,
          overrides: dict[str, int] | None = None) -> dict[str, int]:
    """Remove rows past their retention period. Returns what went, per table.

    Reported per table rather than as a total, because "deleted 40,000 rows" is
    not something anybody can check and "event_outbox: 40000" is.
    """
    now = now or datetime.now(UTC)
    removed: dict[str, int] = {}
    for table, policy in MAP.items():
        if policy.days is None:
            continue
        days = (overrides or {}).get(table, policy.days)
        cutoff = now - timedelta(days=days)
        clauses = [f"{policy.column} < :cutoff"]
        if policy.eligible:
            clauses.append(policy.eligible)
        # S608: every fragment here is a literal from `MAP` in this module. The
        # only caller-supplied value is the cutoff, and it is bound.
        result = session.execute(
            text(f"DELETE FROM {table} WHERE {' AND '.join(clauses)}"),  # noqa: S608
            {"cutoff": cutoff})
        removed[table] = result.rowcount or 0
    session.flush()
    return removed

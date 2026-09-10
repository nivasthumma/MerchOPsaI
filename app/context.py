"""Who is acting, carried explicitly — the execution context.

Every operation runs on behalf of somebody, and "somebody" is not always a
person. Before this module the audit trail could only say `user_id`, and a
worker, a webhook and the reconciliation sweep all wrote with no user at all --
indistinguishable from each other and from a row written by a bug.

    ExecutionContext
        tenant_id, merchant_id    the boundary (enforced by app.tenancy / RLS)
        actor, actor_type         who, and what kind of who
        permissions               what the actor may do, resolved server-side
        correlation_id            the trace this work belongs to
        task_id, incident_id      the work item, when there is one

Actor types:

    HUMAN     an authenticated person, through the API
    AGENT     the bounded agent runtime, acting inside a task a human started
    WORKER    the background worker process (queued tasks, sweeps, webhooks)
    WEBHOOK   a provider delivery, authenticated by signature, not by token
    SYSTEM    everything else: scripts, migrations, the seeder

## Relationship to tenancy

The context does not replace `app.tenancy`; it carries the same scope and
applies it. `bound()` binds both at once, so background work that declares who
it is acting for cannot forget to narrow the database to that merchant.
Background work that legitimately spans merchants declares that too
(`merchant_id=None`) -- the same documented limit app/tenancy.py describes, now
visible on every audit row it writes as WORKER or SYSTEM.
"""
from __future__ import annotations

import enum
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace


class ActorType(str, enum.Enum):
    HUMAN = "HUMAN"
    AGENT = "AGENT"
    WORKER = "WORKER"
    WEBHOOK = "WEBHOOK"
    SYSTEM = "SYSTEM"


@dataclass(frozen=True)
class ExecutionContext:
    actor_type: ActorType
    actor: str | None = None
    tenant_id: str | None = None
    merchant_id: str | None = None
    permissions: tuple[str, ...] = field(default_factory=tuple)
    correlation_id: str | None = None
    task_id: str | None = None
    incident_id: str | None = None

    def as_dict(self) -> dict:
        return {"actor_type": self.actor_type.value, "actor": self.actor,
                "tenant_id": self.tenant_id, "merchant_id": self.merchant_id,
                "permissions": list(self.permissions),
                "correlation_id": self.correlation_id,
                "task_id": self.task_id, "incident_id": self.incident_id}


#: What runs with nothing declared: a script, a migration, a test.
SYSTEM = ExecutionContext(ActorType.SYSTEM, actor="system")

_CURRENT: ContextVar[ExecutionContext] = ContextVar("merchantops_execution", default=SYSTEM)


def current() -> ExecutionContext:
    return _CURRENT.get()


def bind(ctx: ExecutionContext):
    """Set the context for the rest of this task context (a request). Returns
    the token; the request's context dies with the request."""
    return _CURRENT.set(ctx)


def update(**fields) -> None:
    """Refine the current context in place -- e.g. the agent learning its task
    id once the task row exists. Only ever called inside an `acting` block,
    whose exit restores the outer context regardless."""
    _CURRENT.set(replace(current(), **fields))


@contextmanager
def acting(ctx: ExecutionContext) -> Iterator[ExecutionContext]:
    """Run a block as `ctx`. Does not touch the database scope."""
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


@contextmanager
def bound(ctx: ExecutionContext, session=None) -> Iterator[ExecutionContext]:
    """Run a block as `ctx` AND narrow the database to its merchant.

    With a session, the scope is also pushed onto the transaction already open
    on it; transactions begun later pick it up from the begin hook in app.db.
    """
    from app import tenancy

    with acting(ctx), tenancy.scoped(ctx.tenant_id, ctx.merchant_id):
        if session is not None:
            tenancy.apply(session.connection())
        yield ctx


@contextmanager
def as_agent(task_id: str | None, incident_id: str | None = None) -> Iterator[ExecutionContext]:
    """The agent acts inside whoever started it: same scope, same trace, and
    the audit row says AGENT rather than the human whose question it was."""
    outer = current()
    ctx = replace(outer, actor_type=ActorType.AGENT,
                  actor=f"agent:{task_id}" if task_id else "agent",
                  task_id=task_id, incident_id=incident_id or outer.incident_id)
    with acting(ctx):
        yield ctx

"""Incident filtering and saved views — plan P1-05.

The plan lists eleven filters and five saved views. Three things about how they
are built here.

**Filtering is SQL, not a list comprehension.** An operations console that
fetches every incident and filters in Python is one that gets slower as the
merchant gets busier, and — more importantly — one where the merchant scope and
the filter live in different places. Every predicate below is composed into one
statement whose first clause is always the merchant.

**The derived filters are the point.** `severity` and `status` are columns and
would have been easy either way. `approval_required`, `unknown`, `escalated` and
`unresolved` are not: they are facts about the *actions* an incident produced,
reached through the task that produced them. Those are exactly the filters an
operator wants at 2am and exactly the ones a client cannot compute without
fetching the whole action table, so they are answered here.

**Saved views are server-declared.** A view is a named filter combination, and
the plan names five. Declaring them here rather than in the browser means
`My attention` cannot come to mean one thing in a link somebody pasted and
another in the sidebar; and a view added later reaches every client at once.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text

# EXISTS clauses over the actions an incident produced, reached through the task
# that produced them. Written once each: two call sites with two subtly
# different notions of "has an UNKNOWN action" is how a saved view and a filter
# chip come to disagree.
_HAS_UNKNOWN = """
    EXISTS (SELECT 1 FROM agent_tasks t
              JOIN agent_actions a ON a.task_id = t.id
             WHERE t.incident_id = i.id
               AND a.escalated = false
               AND a.verification_state IN ('UNKNOWN', 'PARTIAL'))
"""

_HAS_ESCALATED = """
    EXISTS (SELECT 1 FROM agent_tasks t
              JOIN agent_actions a ON a.task_id = t.id
             WHERE t.incident_id = i.id AND a.escalated = true)
"""

_NEEDS_APPROVAL = """
    EXISTS (SELECT 1 FROM agent_tasks t
              JOIN approvals ap ON ap.task_id = t.id
             WHERE t.incident_id = i.id AND ap.decision = 'PENDING'
               AND ap.expires_at > now())
"""

# "Unresolved" is a property of the incident's own lifecycle, not of its
# actions: an incident can have every action settled and still be open because
# nobody has closed it.
_UNRESOLVED = "i.status NOT IN ('RESOLVED', 'CLOSED', 'CANCELLED')"


@dataclass
class IncidentFilter:
    """The eleven filters P1-05 names. Every field is optional; an empty filter
    is "everything open", which is what the console showed before this existed."""
    severity: list[str] = field(default_factory=list)
    status: list[str] = field(default_factory=list)
    incident_type: list[str] = field(default_factory=list)
    payment_method: list[str] = field(default_factory=list)
    min_amount_minor: int | None = None
    max_age_hours: int | None = None
    unresolved: bool = False
    approval_required: bool = False
    has_unknown: bool = False
    escalated: bool = False
    include_closed: bool = False


# The five saved views, as filter presets. Order is the order they are offered.
SAVED_VIEWS: tuple[dict, ...] = (
    {
        "key": "my_attention",
        "label": "My attention",
        "hint": "Open, and waiting on a person — an approval or an escalation.",
        # Deliberately the union of the two things a human is the only fix for.
        # An incident being merely open is not "my attention"; that is the
        # unfiltered list, and a view that matches everything is not a view.
        "filter": {"unresolved": True, "any_of": ["approval_required", "escalated"]},
    },
    {
        "key": "financial_risk",
        "label": "Financial risk",
        "hint": "Open incidents carrying at least ₹1,000 of revenue at risk.",
        "filter": {"unresolved": True, "min_amount_minor": 100_000},
    },
    {
        "key": "unknown",
        "label": "UNKNOWN",
        "hint": "Incidents whose actions have an unestablished outcome.",
        "filter": {"has_unknown": True},
    },
    {
        "key": "awaiting_approval",
        "label": "Awaiting approval",
        "hint": "A person is the gate. Nothing has reached the provider.",
        "filter": {"approval_required": True},
    },
    {
        "key": "critical",
        "label": "Critical",
        "hint": "Open, and severity CRITICAL.",
        "filter": {"unresolved": True, "severity": ["CRITICAL"]},
    },
)

_VIEWS_BY_KEY = {v["key"]: v for v in SAVED_VIEWS}


def from_view(key: str) -> IncidentFilter | None:
    """Resolve a saved view to its filter, or None if nobody declared it.

    None rather than an empty filter: a link naming a view that no longer
    exists must not silently render as "everything", which reads as though the
    view matched every incident.
    """
    view = _VIEWS_BY_KEY.get(key)
    if view is None:
        return None
    spec = dict(view["filter"])
    any_of = spec.pop("any_of", None)
    f = IncidentFilter(**spec)
    if any_of:
        # `any_of` is the one thing the dataclass cannot express, because its
        # fields are ANDed. Applied by the caller through `any_of` below.
        f = _WithAnyOf(f, tuple(any_of))
    return f


class _WithAnyOf(IncidentFilter):
    """An `IncidentFilter` whose named derived predicates are ORed together.

    Exists for exactly one view — "My attention" is *approval required OR
    escalated* — and is kept a subclass rather than a flag on every filter so
    that the ordinary case stays a plain conjunction with nothing to reason
    about.
    """

    def __init__(self, base: IncidentFilter, any_of: tuple[str, ...]):
        super().__init__(**{k: getattr(base, k) for k in base.__dataclass_fields__})
        self.any_of = any_of


_DERIVED = {
    "approval_required": _NEEDS_APPROVAL,
    "has_unknown": _HAS_UNKNOWN,
    "escalated": _HAS_ESCALATED,
    "unresolved": _UNRESOLVED,
}


def build_query(f: IncidentFilter, merchant_id: str) -> tuple[str, dict]:
    """The WHERE clause and its parameters. Returned rather than executed so a
    caller can count and page over the same predicate it lists by."""
    clauses = ["i.merchant_id = :m"]
    params: dict = {"m": merchant_id}

    if not f.include_closed and not f.unresolved:
        # The console's long-standing default: resolved work listed beside live
        # work is a console nobody reads. `unresolved` says the same thing more
        # strongly and makes this redundant, hence the second condition.
        clauses.append("i.status NOT IN ('RESOLVED', 'CLOSED')")

    if f.severity:
        clauses.append("i.severity = ANY(:sev)")
        params["sev"] = list(f.severity)
    if f.status:
        clauses.append("i.status = ANY(:st)")
        params["st"] = list(f.status)
    if f.incident_type:
        clauses.append("i.incident_type = ANY(:ty)")
        params["ty"] = list(f.incident_type)
    if f.min_amount_minor is not None:
        clauses.append("i.revenue_at_risk_minor >= :amt")
        params["amt"] = int(f.min_amount_minor)
    if f.max_age_hours is not None:
        # Age is measured from detection, not from `started_at`: an incident
        # whose underlying degradation began last week but which was detected
        # an hour ago is an hour old as far as anyone acting on it is concerned.
        clauses.append("i.detected_at >= now() - make_interval(hours => :age)")
        params["age"] = int(f.max_age_hours)

    if f.payment_method:
        # Reached through the recovery candidates, which are the only place an
        # incident is tied to a payment method. An incident with no plan yet
        # therefore matches no method filter, which is correct: nothing has
        # attributed it to one.
        clauses.append("""
            EXISTS (SELECT 1 FROM recovery_candidates c
                      JOIN payments p ON p.id = c.payment_id
                     WHERE c.incident_id = i.id AND p.method = ANY(:meth))
        """)
        params["meth"] = list(f.payment_method)

    any_of = tuple(getattr(f, "any_of", ()) or ())
    for name, sql in _DERIVED.items():
        if name in any_of:
            continue
        if getattr(f, name, False):
            clauses.append(sql)
    if any_of:
        clauses.append("(" + " OR ".join(_DERIVED[n] for n in any_of) + ")")

    return " AND ".join(clauses), params


def search_incidents(session, merchant_id: str, f: IncidentFilter,
                     *, limit: int = 200) -> list[str]:
    """The ids that match, ordered as the console orders them.

    Ids rather than rows: the caller already has `_incident_view` and loading
    twice would be cheaper to write and slower to run than letting it load once
    by primary key.
    """
    where, params = build_query(f, merchant_id)
    sql = f"""
        SELECT i.id FROM incidents i
         WHERE {where}
         ORDER BY i.revenue_at_risk_minor DESC, i.detected_at DESC
         LIMIT :lim
    """  # noqa: S608 - see the module docstring: every fragment above is a
    # literal written in this file, and every caller-supplied value is bound.
    return list(session.execute(text(sql), {**params, "lim": limit}).scalars())


def view_counts(session, merchant_id: str) -> dict[str, int]:
    """How many incidents each saved view holds.

    Published so a view can show its count without the client fetching each
    view in turn — five requests to render five numbers, each from a different
    instant.
    """
    counts: dict[str, int] = {}
    for view in SAVED_VIEWS:
        f = from_view(view["key"])
        where, params = build_query(f, merchant_id)  # type: ignore[arg-type]
        sql = f"SELECT COUNT(*) FROM incidents i WHERE {where}"  # noqa: S608
        counts[view["key"]] = int(session.execute(text(sql), params).scalar() or 0)
    return counts

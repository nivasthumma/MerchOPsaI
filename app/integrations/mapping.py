"""The synthetic→external mapping layer — MerchantOps §6, plan P0-02.

    internal payment id
      → provider_mappings
      → provider
      → environment
      → external payment id

This module is the *only* path from an id this system minted to an id the
provider recognises. It exists because of one failure mode, stated plainly in
the plan:

> Never allow a synthetic ID to resolve accidentally to another provider
> payment.

## Why a table rather than the column that was already there

`payments.external_payment_id` is a nullable `String(64)` with a plain index.
It resolves, and it cannot promise anything:

* Nothing stopped two internal payments carrying the same external id. The
  first refund would move money against a payment the second one also claims.
* It records no provider. One column cannot hold a Razorpay id and a Stripe id
  and stay meaningful.
* It records no **environment**, which is the dangerous one. A Test Mode id and
  a live id are both short strings beginning `pay_`. Nothing in the column, and
  nothing in the code reading it, distinguished a rehearsal from real money —
  the process's own credentials decided, at call time, which universe the id
  was interpreted in.

`provider_mappings` carries provider and environment on the row, and holds both
directions unique in the database (see `app.models.ProviderMapping`). The
column stays: it is the mock adapter's own store — the mock *is* the provider,
so it owning a provider-side id is right — and `check_consistency` below exists
so the two can be asserted to agree rather than assumed to.

## What the model may do

Read, through `resolve`. Nothing here accepts an external id from tool
arguments, from model output, or from a request body. `resolve` takes an
internal id and the caller's merchant, and returns either one mapping or a
refusal with a machine-readable reason.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import get_settings

# The environments a mapping may name. Not free text: an environment nobody
# recognises must fail the write, not sit in the table resolving to nothing.
ENVIRONMENTS = ("test", "live")


def active_environment() -> str:
    """Which provider universe this process is operating in.

    Derived from the resolved adapter mode rather than from a separate
    environment variable, so it cannot disagree with the adapter that will
    actually place the call.

    Both modes this build ships — `mock` and `live_test_mode` — are `test`. The
    mock stands in for Test Mode, so a mapping seeded against it resolves
    unchanged the moment real Test Mode credentials are supplied; that is the
    point of the spike being a credential change and not a data migration.
    `live` is reserved and no adapter currently returns it, which is why the
    comparison is exact: a mode nobody has implemented must not be guessed into
    the live universe by a prefix match.
    """
    return "live" if get_settings().resolved_razorpay_mode == "live" else "test"


@dataclass(frozen=True)
class Mapping:
    """One resolved mapping. Immutable: callers pass it around as evidence of
    what was resolved, and a mutable one could be edited between the resolution
    and the call it authorises."""
    payment_id: str
    merchant_id: str
    provider: str
    environment: str
    external_payment_id: str
    source: str
    verified_at: datetime | None


@dataclass(frozen=True)
class MappingFailure:
    """Why resolution refused. `code` is stable and is what tests, the failure
    taxonomy and the UI key on; `detail` is for a human."""
    code: str
    detail: str

    def as_dict(self) -> dict:
        return {"error": self.code, "detail": self.detail}


# The refusal codes. Named here so the set is enumerable — a caller can render
# every one of them, and a new code cannot be introduced by a typo at a call
# site.
UNKNOWN_PAYMENT = "unknown_payment"
MERCHANT_ISOLATION = "merchant_isolation"
NOT_MAPPED = "not_externally_mapped"
WRONG_ENVIRONMENT = "mapped_to_other_environment"
RETIRED = "mapping_retired"

REFUSAL_CODES = (UNKNOWN_PAYMENT, MERCHANT_ISOLATION, NOT_MAPPED,
                 WRONG_ENVIRONMENT, RETIRED)


def resolve(session, merchant_id: str, payment_id: str, *,
            provider: str = "razorpay",
            environment: str | None = None) -> tuple[Mapping | None, MappingFailure | None]:
    """The one way to get from an internal payment id to a provider id.

    Returns exactly one of `(mapping, None)` or `(None, failure)`. There is no
    third outcome and no partial one: a caller that gets a mapping back may
    execute against it, and a caller that gets a failure may not proceed by
    guessing.

    The merchant check comes before the mapping check on purpose. A caller
    asking about another merchant's payment must be told it is not theirs and
    must not learn, from a different refusal code, whether that payment happens
    to be externally mapped.
    """
    env = environment or active_environment()

    owner = session.execute(
        text("SELECT merchant_id FROM payments WHERE id = :p"),
        {"p": payment_id},
    ).scalar()
    if owner is None:
        return None, MappingFailure(
            UNKNOWN_PAYMENT, f"No payment {payment_id} exists.")
    if owner != merchant_id:
        return None, MappingFailure(
            MERCHANT_ISOLATION,
            f"Payment {payment_id} does not belong to {merchant_id}.")

    row = session.execute(text("""
        SELECT payment_id, merchant_id, provider, environment, external_payment_id,
               status, source, verified_at
        FROM provider_mappings
        WHERE payment_id = :p AND provider = :prov AND environment = :env
    """), {"p": payment_id, "prov": provider, "env": env}).mappings().first()

    if row is None:
        # Distinguish "no mapping anywhere" from "mapped, but into the other
        # universe". They look the same to a naive query and they are not the
        # same problem: the second one means somebody pointed a live process at
        # test-mode data, and reporting it as "unmapped" would hide that.
        other = session.execute(text("""
            SELECT environment FROM provider_mappings
            WHERE payment_id = :p AND provider = :prov
        """), {"p": payment_id, "prov": provider}).scalars().all()
        if other:
            return None, MappingFailure(
                WRONG_ENVIRONMENT,
                f"{payment_id} is mapped for {provider} in "
                f"{', '.join(sorted(set(other)))} but this process is operating "
                f"in {env}. Executing against it would act in the wrong "
                f"environment.")
        return None, MappingFailure(
            NOT_MAPPED,
            f"{payment_id} has no {provider} mapping in {env}. Only the mapped "
            f"subset can be executed externally.")

    if row["status"] != "ACTIVE":
        return None, MappingFailure(
            RETIRED,
            f"The {provider} mapping for {payment_id} is {row['status']} and "
            f"must not be executed against.")

    return Mapping(
        payment_id=row["payment_id"], merchant_id=row["merchant_id"],
        provider=row["provider"], environment=row["environment"],
        external_payment_id=row["external_payment_id"],
        source=row["source"], verified_at=row["verified_at"],
    ), None


def record_mapping(session, *, merchant_id: str, payment_id: str,
                   external_payment_id: str, provider: str = "razorpay",
                   environment: str | None = None, source: str = "seed",
                   verified_at: datetime | None = None) -> str:
    """Create or update one mapping. Not reachable from the model.

    Idempotent on (payment_id, provider, environment): re-recording the same
    external id is a no-op, and recording a *different* one for a payment that
    already has one is an update with the previous value written to the audit
    trail by the caller. The other direction — pointing a second payment at an
    external id already claimed — is refused by
    `uq_mapping_external_identity`, and that IntegrityError is deliberately not
    caught here. It means two internal payments claim one provider object, and
    the correct handling of that is to fail loudly.
    """
    env = environment or active_environment()
    if env not in ENVIRONMENTS:
        raise ValueError(f"Unknown provider environment {env!r}; expected one of "
                         f"{', '.join(ENVIRONMENTS)}.")

    existing = session.execute(text("""
        SELECT id, external_payment_id FROM provider_mappings
        WHERE payment_id = :p AND provider = :prov AND environment = :env
    """), {"p": payment_id, "prov": provider, "env": env}).mappings().first()

    if existing is not None:
        session.execute(text("""
            UPDATE provider_mappings
               SET external_payment_id = :e, source = :s, status = 'ACTIVE',
                   verified_at = :v
             WHERE id = :id
        """), {"e": external_payment_id, "s": source, "v": verified_at,
               "id": existing["id"]})
        return existing["id"]

    mapping_id = f"PMP_{uuid.uuid4().hex[:12].upper()}"
    session.execute(text("""
        INSERT INTO provider_mappings
            (id, merchant_id, payment_id, provider, environment,
             external_payment_id, status, source, verified_at, created_at)
        VALUES (:id, :m, :p, :prov, :env, :e, 'ACTIVE', :s, :v, :now)
    """), {"id": mapping_id, "m": merchant_id, "p": payment_id, "prov": provider,
           "env": env, "e": external_payment_id, "s": source, "v": verified_at,
           "now": datetime.now(UTC)})
    return mapping_id


def check_consistency(session, *, provider: str = "razorpay",
                      environment: str | None = None) -> list[dict]:
    """Every place `payments.external_payment_id` and `provider_mappings`
    disagree.

    Two authorities on one fact is a defect waiting for the day they diverge.
    This build keeps both — the column is the mock provider's store, the table
    is the control plane's mapping — so the disagreement is made *checkable*
    rather than assumed away. An empty list is the only acceptable result, and
    `/readiness` reports it.
    """
    env = environment or active_environment()
    rows = session.execute(text("""
        SELECT p.id AS payment_id,
               p.external_payment_id AS column_value,
               m.external_payment_id AS mapping_value
          FROM payments p
          FULL OUTER JOIN provider_mappings m
            ON m.payment_id = p.id AND m.provider = :prov AND m.environment = :env
         WHERE (p.external_payment_id IS NOT NULL OR m.external_payment_id IS NOT NULL)
           AND (p.external_payment_id IS DISTINCT FROM m.external_payment_id)
    """), {"prov": provider, "env": env}).mappings().all()
    return [dict(r) for r in rows]


def mapping_coverage(session, merchant_id: str | None = None, *,
                     provider: str = "razorpay",
                     environment: str | None = None) -> dict:
    """How much of the payment set can actually be executed against.

    Published rather than inferred. "17 refunds were rejected" is ambiguous
    between a broken mapping layer and a working one applied to unmapped data,
    and the difference matters enough that the number is on the readiness
    report.
    """
    env = environment or active_environment()
    params: dict = {"prov": provider, "env": env}
    clause = ""
    if merchant_id:
        clause = " AND p.merchant_id = :m"
        params["m"] = merchant_id

    # S608 (SQL built by interpolation) is suppressed on a structural argument
    # rather than a promise: `clause` is assigned from exactly two string
    # literals a few lines above and can hold nothing else. `merchant_id` — the
    # only caller-supplied value — is bound as :m and never interpolated.
    sql = f"""
        SELECT COUNT(*) AS payments,
               COUNT(m.id) AS mapped
          FROM payments p
          LEFT JOIN provider_mappings m
            ON m.payment_id = p.id AND m.provider = :prov
           AND m.environment = :env AND m.status = 'ACTIVE'
         WHERE 1 = 1{clause}
    """  # noqa: S608
    row = session.execute(text(sql), params).mappings().first()

    payments = int(row["payments"] or 0)
    mapped = int(row["mapped"] or 0)
    return {
        "provider": provider,
        "environment": env,
        "payments": payments,
        "mapped": mapped,
        # Deliberately null rather than 0.0 over an empty set: a coverage of
        # "none of nothing" is not a coverage of zero.
        "coverage": (mapped / payments) if payments else None,
    }

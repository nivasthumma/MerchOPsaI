"""Unified application idempotency.

    same key + same request       -> same outcome
    same key + different request  -> conflict

Before this, idempotency in MerchantOps was a set of UNIQUE constraints, one per
table, each on a key alone. A constraint on a key can say "this key was used"; it
cannot say "this key was used for a DIFFERENT request", which is the case that
matters -- a retry that quietly changed the amount would collide on the key and
be reported as an ordinary duplicate, and the difference would never surface.

An `IdempotencyRecord` remembers the request its key was first used for, as a
hash, and refuses a second use that does not match. It also remembers what the
first request produced (`resource_type`, `resource_id`, `external_reference`,
`status`) so a replay can be answered with the original outcome.

## Two layers, both kept

    MerchantOps idempotency   this module, and the UNIQUE keys it sits beside
    provider idempotency      the key sent to Razorpay with the call itself
                              (`X-Refund-Idempotency`, a link's `reference_id`)

The provider's protects the provider call; this protects everything on our side
of it. Neither replaces the other.

## Keys are server-derived

Every financial key is derived from server-held facts (tools/actions.py
`derive_idempotency_key`), never taken from a model or a request body. The one
client-supplied key is the HTTP `Idempotency-Key` header, which is scoped to the
authenticated principal and to the operation it names.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.models import AgentAction, IdempotencyRecord, VerificationState

IN_PROGRESS = "IN_PROGRESS"
SUCCEEDED = "SUCCEEDED"
FAILED = "FAILED"
UNKNOWN = "UNKNOWN"


class IdempotencyConflict(Exception):
    """The key was already used for a different request."""

    def __init__(self, record: IdempotencyRecord):
        super().__init__(
            f"Idempotency key already used for a different {record.operation} "
            f"request (first used {record.created_at:%Y-%m-%d %H:%M:%S} UTC). "
            f"Refusing: a changed request under a reused key is never a retry.")
        self.record = record


def request_hash(request: dict) -> str:
    """Canonical JSON, so key order and whitespace are not a difference."""
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Claim:
    record: IdempotencyRecord
    #: True when this call created the record -- the first use of the key.
    fresh: bool


def _find(session, merchant_id: str, operation: str, business_key: str,
          *, lock: bool = False):
    q = (session.query(IdempotencyRecord)
         .filter(IdempotencyRecord.merchant_id == merchant_id,
                 IdempotencyRecord.operation == operation,
                 IdempotencyRecord.business_key == business_key))
    return (q.with_for_update() if lock else q).first()


def _check(existing: IdempotencyRecord, digest: str) -> Claim:
    if existing.request_hash != digest:
        raise IdempotencyConflict(existing)
    return Claim(existing, False)


def claim(session, *, merchant_id: str, operation: str, business_key: str,
          request: dict, tenant_id: str | None = None,
          ttl_seconds: int | None = None) -> Claim:
    """Claim `business_key` for `operation`, or learn it was already claimed.

    Raises `IdempotencyConflict` when the key was used for a different request.
    `ttl_seconds=None` means the key never expires, which is the only correct
    setting for an operation that can move money.
    """
    digest = request_hash(request)
    now = datetime.now(UTC)
    existing = _find(session, merchant_id, operation, business_key)
    if existing is not None:
        if existing.expires_at is not None and existing.expires_at <= now:
            # An expired non-financial key is free again: the record is reused
            # for the new request rather than kept beside it. Re-read under a
            # row lock, so two callers cannot both take the same expired key.
            existing = _find(session, merchant_id, operation, business_key, lock=True)
            if existing.expires_at is None or existing.expires_at > now:
                return _check(existing, digest)
            existing.request_hash = digest
            existing.status = IN_PROGRESS
            existing.external_reference = existing.resource_id = None
            existing.resource_type = None
            existing.expires_at = (now + timedelta(seconds=ttl_seconds)
                                   if ttl_seconds else None)
            session.flush()
            return Claim(existing, True)
        return _check(existing, digest)

    record = IdempotencyRecord(
        id=f"IDEM_{uuid.uuid4().hex[:16].upper()}", tenant_id=tenant_id,
        merchant_id=merchant_id, operation=operation, business_key=business_key,
        request_hash=digest, status=IN_PROGRESS,
        expires_at=(now + timedelta(seconds=ttl_seconds)) if ttl_seconds else None,
    )
    # Attempted, not pre-checked alone: two concurrent first uses both pass the
    # SELECT above, and only the constraint can say which one won.
    sp = session.begin_nested()
    try:
        session.add(record)
        session.flush()
        sp.commit()
    except IntegrityError:
        sp.rollback()
        existing = _find(session, merchant_id, operation, business_key)
        if existing is None:        # the winner rolled back; nothing to compare
            raise
        return _check(existing, digest)
    return Claim(record, True)


def settle(record: IdempotencyRecord, *, status: str,
           external_reference: str | None = None,
           resource_type: str | None = None, resource_id: str | None = None) -> None:
    record.status = status
    if external_reference is not None:
        record.external_reference = external_reference
    if resource_type is not None:
        record.resource_type = resource_type
    if resource_id is not None:
        record.resource_id = resource_id


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def action_operation(action_type: str) -> str:
    return f"action.{action_type}"


_STATUS_FOR = {
    VerificationState.SUCCESS: SUCCEEDED,
    VerificationState.FAILED: FAILED,
    VerificationState.PARTIAL: UNKNOWN,
    VerificationState.UNKNOWN: UNKNOWN,
}


def sync_from_action(session, action: AgentAction) -> None:
    """Carry an action's settled state onto its idempotency record.

    Called wherever an action's verification is recorded
    (verification/schedule.py `record_attempt`), so the record says what the
    provider was last established to have done, not what was hoped.
    """
    record = _find(session, action.merchant_id, action_operation(action.action_type),
                   action.idempotency_key)
    if record is None:
        return
    status = _STATUS_FOR.get(action.verification_state, IN_PROGRESS) \
        if action.verification_state is not None else IN_PROGRESS
    settle(record, status=status, external_reference=action.external_reference,
           resource_type="agent_action", resource_id=action.id)

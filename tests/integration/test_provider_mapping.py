"""The mapping layer — MerchantOps §6, plan P0-02.

The property under test is not "resolution works". It is that resolution
**cannot** land on the wrong provider object, which is a claim about
constraints rather than about code paths. So most of these tests try to write
the bad state and assert the database refuses, rather than calling a function
and checking its return.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.integrations.mapping import (
    MERCHANT_ISOLATION,
    NOT_MAPPED,
    UNKNOWN_PAYMENT,
    WRONG_ENVIRONMENT,
    active_environment,
    check_consistency,
    mapping_coverage,
    record_mapping,
)
from app.integrations.mapping import resolve as resolve_mapping

MAPPED = "SYN_PAY_0001"
UNMAPPED = "SYN_PAY_0009"      # deliberately unmapped in the seed


def test_a_mapped_payment_resolves_to_its_own_external_id(db):
    mapping, failure = resolve_mapping(db, "MERCH_A", MAPPED)
    assert failure is None
    assert mapping.payment_id == MAPPED
    assert mapping.provider == "razorpay"
    assert mapping.environment == "test"
    assert mapping.external_payment_id.startswith("pay_")


def test_an_unmapped_payment_is_refused_rather_than_guessed(db):
    """The rejection path is the mapping layer working, not a defect. A payment
    with no provider object cannot be executed against, and inventing an id for
    it is the one thing §6 forbids."""
    mapping, failure = resolve_mapping(db, "MERCH_A", UNMAPPED)
    assert mapping is None
    assert failure.code == NOT_MAPPED


def test_an_unknown_payment_and_another_merchants_payment_read_differently(db):
    """...to us. To the caller they must not: a caller asking about another
    merchant's payment is told it is not theirs, and learns nothing about
    whether it happens to be mapped."""
    _, unknown = resolve_mapping(db, "MERCH_A", "SYN_PAY_DOES_NOT_EXIST")
    assert unknown.code == UNKNOWN_PAYMENT

    # A real payment, owned by someone else. The merchant check runs BEFORE the
    # mapping lookup, so the refusal is about ownership and never leaks whether
    # a mapping exists.
    _, foreign = resolve_mapping(db, "MERCH_B", MAPPED)
    assert foreign.code == MERCHANT_ISOLATION


def test_two_payments_cannot_claim_one_provider_object(db):
    """The direction that matters. Before `provider_mappings` this was an
    ordinary nullable column, and nothing stopped a second internal payment
    naming an external id already in use -- so a refund for one payment could
    move money against another."""
    taken = db.execute(text(
        "SELECT external_payment_id FROM provider_mappings WHERE payment_id = :p"),
        {"p": MAPPED}).scalar()

    with pytest.raises(IntegrityError):
        record_mapping(db, merchant_id="MERCH_A", payment_id=UNMAPPED,
                       external_payment_id=taken, source="test")
        db.flush()
    db.rollback()


def test_one_payment_cannot_hold_two_ids_in_one_environment(db):
    """Resolution is a function. A payment with two external ids in the same
    universe is a query that might return either, and `resolve` would have to
    pick -- which is a decision no code should be making about money."""
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO provider_mappings
                (id, merchant_id, payment_id, provider, environment,
                 external_payment_id, status, source, created_at)
            VALUES ('PMP_DUP', 'MERCH_A', :p, 'razorpay', 'test',
                    'pay_SECONDCLAIM', 'ACTIVE', 'test', now())
        """), {"p": MAPPED})
        db.flush()
    db.rollback()


def test_the_same_external_id_may_exist_in_both_environments(db):
    """Test and live are different universes. An id that means one thing in
    Test Mode and another in live is not a conflict -- treating it as one would
    forbid the ordinary case of promoting a mapping."""
    external = db.execute(text(
        "SELECT external_payment_id FROM provider_mappings WHERE payment_id = :p"),
        {"p": MAPPED}).scalar()

    record_mapping(db, merchant_id="MERCH_A", payment_id=MAPPED,
                   external_payment_id=external, environment="live",
                   source="test")
    db.flush()

    rows = db.execute(text(
        "SELECT environment FROM provider_mappings WHERE payment_id = :p"),
        {"p": MAPPED}).scalars().all()
    assert sorted(rows) == ["live", "test"]


def test_a_mapping_in_the_other_environment_is_not_reported_as_missing(db):
    """The distinction that stops a live process quietly acting on test data.

    Both look like "no mapping" to a naive query, and they are not the same
    problem: one is unmapped data, the other is a process pointed at the wrong
    universe."""
    record_mapping(db, merchant_id="MERCH_A", payment_id=UNMAPPED,
                   external_payment_id="pay_LIVEONLY0001", environment="live",
                   source="test")
    db.flush()

    mapping, failure = resolve_mapping(db, "MERCH_A", UNMAPPED,
                                       environment="test")
    assert mapping is None
    assert failure.code == WRONG_ENVIRONMENT
    assert "live" in failure.detail


def test_a_retired_mapping_does_not_resolve(db):
    db.execute(text("UPDATE provider_mappings SET status = 'RETIRED' "
                    "WHERE payment_id = :p"), {"p": MAPPED})
    db.flush()
    mapping, failure = resolve_mapping(db, "MERCH_A", MAPPED)
    assert mapping is None
    assert failure.code == "mapping_retired"


def test_an_unknown_environment_is_refused_by_the_database(db):
    with pytest.raises(IntegrityError):
        db.execute(text("""
            INSERT INTO provider_mappings
                (id, merchant_id, payment_id, provider, environment,
                 external_payment_id, status, source, created_at)
            VALUES ('PMP_BADENV', 'MERCH_A', :p, 'razorpay', 'sandbox',
                    'pay_BADENV0000001', 'ACTIVE', 'test', now())
        """), {"p": UNMAPPED})
        db.flush()
    db.rollback()


def test_recording_the_same_mapping_twice_is_a_no_op(db):
    external = db.execute(text(
        "SELECT external_payment_id FROM provider_mappings WHERE payment_id = :p"),
        {"p": MAPPED}).scalar()
    before = db.execute(text("SELECT COUNT(*) FROM provider_mappings")).scalar()

    record_mapping(db, merchant_id="MERCH_A", payment_id=MAPPED,
                   external_payment_id=external, source="test")
    db.flush()

    assert db.execute(text("SELECT COUNT(*) FROM provider_mappings")).scalar() == before


def test_the_seed_leaves_the_table_and_the_column_agreeing(db):
    """Two authorities on one fact, kept deliberately and therefore checked.
    An empty list is the only acceptable result."""
    assert check_consistency(db) == []


def test_coverage_is_published_rather_than_inferred(db):
    """"Seventeen refunds were rejected" is ambiguous between a broken mapping
    layer and a working one applied to unmapped data."""
    cov = mapping_coverage(db, "MERCH_A")
    assert cov["environment"] == active_environment()
    assert cov["mapped"] > 0
    assert cov["payments"] > cov["mapped"], "the seed keeps most payments unmapped"
    assert 0 < cov["coverage"] < 1


def test_coverage_over_no_payments_is_unknown_not_zero(db):
    """A coverage of "none of nothing" is not a coverage of zero."""
    cov = mapping_coverage(db, "MERCHANT_THAT_DOES_NOT_EXIST")
    assert cov["payments"] == 0
    assert cov["coverage"] is None


def test_the_refund_path_refuses_an_unmapped_payment(db, owner):
    """End to end: the mapping layer is what the executor consults, not a
    column it reads itself."""
    from app.tools.actions import resolve_external_payment

    external, meta = resolve_external_payment(db, "MERCH_A", UNMAPPED)
    assert external is None
    assert meta["error"] == NOT_MAPPED

    external, meta = resolve_external_payment(db, "MERCH_A", MAPPED)
    assert external.startswith("pay_")
    # The universe the call will be placed in, carried alongside the id so the
    # caller need not ask the settings a second time.
    assert meta["environment"] == "test"
    assert meta["provider"] == "razorpay"


def test_a_deleted_mapping_stops_execution_even_though_the_column_survives(db, owner):
    """The proof that the table is the authority and the column is not.

    `payments.external_payment_id` is left exactly as it was; only the mapping
    row goes. If the executor were still reading the column this would happily
    refund."""
    from app.tools.actions import resolve_external_payment

    db.execute(text("DELETE FROM provider_mappings WHERE payment_id = :p"),
               {"p": MAPPED})
    db.flush()

    still_there = db.execute(text(
        "SELECT external_payment_id FROM payments WHERE id = :p"),
        {"p": MAPPED}).scalar()
    assert still_there, "the column is deliberately untouched by this test"

    external, meta = resolve_external_payment(db, "MERCH_A", MAPPED)
    assert external is None
    assert meta["error"] == NOT_MAPPED


def test_the_verification_tool_resolves_through_the_same_layer(db, owner):
    """A read and a write that resolve an internal id differently is how
    "verified the wrong payment" happens."""
    from app.tools.verification_tools import get_payment_status

    ok = get_payment_status(db, "MERCH_A", MAPPED)
    assert ok.success

    refused = get_payment_status(db, "MERCH_A", UNMAPPED)
    assert not refused.success
    assert refused.data["error"] == NOT_MAPPED

    # Another merchant's payment is NOT_FOUND, not "unmapped": the mapping
    # layer's ownership check runs first and the tool preserves that ordering.
    foreign = get_payment_status(db, "MERCH_B", MAPPED)
    assert not foreign.success
    assert foreign.error_code == "NOT_FOUND"

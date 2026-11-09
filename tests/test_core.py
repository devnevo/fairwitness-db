from __future__ import annotations

import pytest
from conftest import scalar

from fairwitness import FairWitness, FairWitnessError, Proposal, refunds
from fairwitness.refunds import RefundOrder


def refund(order_id: int, cents: int, op_id: str, observed=()) -> Proposal:
    return Proposal(RefundOrder(order_id=order_id, amount_cents=cents), op_id, tuple(observed))


def test_commit_applies_effect_and_stores_receipt(db, fairwitness):
    receipt = fairwitness.commit(refund(1, 3000, "a"))
    assert receipt.committed and receipt.reason is None
    assert receipt.effects["refund_id"] == 1
    assert scalar(db, "SELECT refunded_cents FROM orders WHERE id = 1") == 3000
    assert scalar(db, "SELECT used_cents FROM allowances") == 3000
    assert fairwitness.receipt("a") == receipt


def test_rejection_is_recorded_and_writes_nothing(db, fairwitness):
    refunds.open_dispute(db, 1)
    receipt = fairwitness.commit(refund(1, 3000, "a"))
    assert receipt.status == "rejected"
    assert receipt.reason == "order_disputed"
    assert receipt.effects is None
    assert scalar(db, "SELECT COUNT(*) FROM refunds") == 0
    assert scalar(db, "SELECT used_cents FROM allowances") == 0
    assert fairwitness.receipt("a").reason == "order_disputed"


def test_checks_stop_at_first_failure(fairwitness):
    receipt = fairwitness.commit(refund(99, 3000, "a"))
    assert receipt.reason == "order_not_found"
    assert [c["reason"] for c in receipt.checks] == ["invalid_amount", "order_not_found"]
    assert [c["passed"] for c in receipt.checks] == [True, False]


@pytest.mark.parametrize(
    ("order_id", "cents", "reason"),
    [
        (1, 0, "invalid_amount"),
        (3, 11_000, "exceeds_policy_cap"),
        (2, 4600, "exceeds_remaining"),
    ],
)
def test_each_rule_has_an_actionable_reason(fairwitness, order_id, cents, reason):
    assert fairwitness.commit(refund(order_id, cents, "a")).reason == reason


def test_allowance_exhausted(db):
    refunds.set_policy_cap(db, 50_000)
    r = refunds.fairwitness(db)
    assert r.commit(refund(3, 12_000, "a")).committed
    assert r.commit(refund(1, 8000, "b")).committed  # used = 20_000 = limit
    assert r.commit(refund(2, 100, "c")).reason == "allowance_exhausted"
    r.close()


def test_snapshot_is_stable_while_others_write(db, fairwitness):
    with fairwitness.snapshot() as snap:
        assert snap.read("orders", 1)["status"] == "paid"
        refunds.open_dispute(db, 1)
        assert snap.read("orders", 1)["status"] == "paid"  # same snapshot
    with fairwitness.snapshot() as snap:
        assert snap.read("orders", 1)["status"] == "disputed"


def test_drift_lists_changed_fields(db, fairwitness):
    with fairwitness.snapshot() as snap:
        snap.read("orders", 1)
        snap.read("orders", 2)  # unchanged: no drift entries
        proposal = snap.propose(RefundOrder(order_id=1, amount_cents=1000), op_id="a")
    refunds.open_dispute(db, 1)
    receipt = fairwitness.commit(proposal)
    fields = {(d["key"], d["field"]): (d["observed"], d["current"]) for d in receipt.drift}
    assert fields[(1, "status")] == ("paid", "disputed")
    assert all(key == 1 for key, _ in fields)


def test_drift_reports_rows_that_appear(db, fairwitness):
    with fairwitness.snapshot() as snap:
        assert snap.read("orders", 7) is None
        proposal = snap.propose(RefundOrder(order_id=7, amount_cents=100), op_id="a")
    conn = refunds.connect(db)
    conn.execute("INSERT INTO orders (id, customer, amount_cents) VALUES (7, 'dee', 500)")
    conn.close()
    receipt = fairwitness.commit(proposal)
    assert receipt.committed
    assert receipt.drift[0]["field"] == "*"
    assert (receipt.drift[0]["observed"], receipt.drift[0]["current"]) == ("absent", "present")


def test_strict_mode_rejects_any_drift_default_mode_revalidates(db):
    loose, strict = refunds.fairwitness(db), refunds.fairwitness(db, strict=True)
    with loose.snapshot() as snap:
        snap.read("allowances", "refunds_daily")
        p1 = snap.propose(RefundOrder(order_id=2, amount_cents=1000), op_id="loose")
        p2 = snap.propose(RefundOrder(order_id=2, amount_cents=1000), op_id="strict")
    assert loose.commit(refund(1, 500, "other")).committed  # moves the allowance
    assert loose.commit(p1).committed
    assert strict.commit(p2).reason == "stale_evidence"
    loose.close()
    strict.close()


def test_snapshot_rejects_reads_after_close(fairwitness):
    snap = fairwitness.snapshot()
    snap.close()
    with pytest.raises(FairWitnessError):
        snap.read("orders", 1)
    assert snap.propose(RefundOrder(order_id=1, amount_cents=1)).observed == ()


def test_unsafe_identifiers_are_refused(fairwitness):
    with fairwitness.snapshot() as snap, pytest.raises(ValueError):
        snap.read("orders; DROP TABLE orders", 1)


def test_unknown_table_is_an_error(fairwitness):
    with fairwitness.snapshot() as snap, pytest.raises(FairWitnessError):
        snap.read("nope", 1)


def test_memory_database_is_refused():
    with pytest.raises(ValueError):
        FairWitness(":memory:")


def test_receipts_are_listed_newest_first(fairwitness):
    for i in range(3):
        fairwitness.commit(refund(1, 100, f"op{i}"))
    assert [r.op_id for r in fairwitness.receipts()] == ["op2", "op1", "op0"]


def test_operation_must_be_a_dataclass(fairwitness):
    class NotADataclass(RefundOrder.__mro__[1]):  # Operation
        kind = "bad"

    with pytest.raises(TypeError):
        fairwitness.commit(Proposal(NotADataclass(), "x"))

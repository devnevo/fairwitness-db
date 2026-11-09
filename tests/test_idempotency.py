from __future__ import annotations

import threading

import pytest
from conftest import scalar

from fairwitness import IdempotencyConflict, Proposal, refunds
from fairwitness.refunds import RefundOrder


def test_retry_after_lost_response_replays_the_receipt(db, fairwitness):
    proposal = Proposal(RefundOrder(order_id=1, amount_cents=3000), "task-7:refund")
    first = fairwitness.commit(proposal)
    again = fairwitness.commit(proposal)
    assert again.replayed and not first.replayed
    assert again.effects == first.effects
    assert scalar(db, "SELECT COUNT(*) FROM refunds") == 1
    assert scalar(db, "SELECT used_cents FROM allowances") == 3000


def test_same_op_id_with_different_args_is_a_conflict(db, fairwitness):
    fairwitness.commit(Proposal(RefundOrder(order_id=1, amount_cents=3000), "task-7:refund"))
    with pytest.raises(IdempotencyConflict) as err:
        fairwitness.commit(Proposal(RefundOrder(order_id=1, amount_cents=2999), "task-7:refund"))
    assert err.value.op_id == "task-7:refund"
    assert scalar(db, "SELECT COUNT(*) FROM refunds") == 1


def test_a_rejection_is_final_for_its_op_id(db, fairwitness):
    refunds.open_dispute(db, 1)
    proposal = Proposal(RefundOrder(order_id=1, amount_cents=3000), "task-7:refund")
    assert fairwitness.commit(proposal).reason == "order_disputed"
    conn = refunds.connect(db)
    conn.execute("UPDATE orders SET status = 'paid' WHERE id = 1")  # dispute resolved
    conn.close()
    replay = fairwitness.commit(proposal)
    assert replay.replayed and replay.reason == "order_disputed"
    fresh = Proposal(RefundOrder(order_id=1, amount_cents=3000), "task-7:refund#2")
    assert fairwitness.commit(fresh).committed


def test_concurrent_duplicates_apply_once(db):
    proposal = Proposal(RefundOrder(order_id=3, amount_cents=5000), "dup")
    results, errors = [], []

    def worker():
        r = refunds.fairwitness(db)
        try:
            results.append(r.commit(proposal))
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            r.close()

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sum(not r.replayed for r in results) == 1
    assert scalar(db, "SELECT COUNT(*) FROM refunds") == 1

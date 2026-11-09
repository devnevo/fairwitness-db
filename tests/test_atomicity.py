"""A crash at any point inside commit leaves no effect and no receipt."""

from __future__ import annotations

import pytest
from conftest import scalar

from fairwitness import Proposal, refunds
from fairwitness.core import FAULT_POINTS
from fairwitness.refunds import RefundOrder


class Crash(Exception):
    pass


@pytest.mark.parametrize("point", FAULT_POINTS)
def test_crash_mid_commit_rolls_back_everything(db, point):
    def hook(where, _proposal):
        if where == point:
            raise Crash(where)

    crashing = refunds.fairwitness(db, fault_hook=hook)
    proposal = Proposal(RefundOrder(order_id=1, amount_cents=3000), "task-9:refund")
    with pytest.raises(Crash):
        crashing.commit(proposal)
    crashing.close()

    assert scalar(db, "SELECT COUNT(*) FROM refunds") == 0
    assert scalar(db, "SELECT refunded_cents FROM orders WHERE id = 1") == 0
    assert scalar(db, "SELECT used_cents FROM allowances") == 0
    assert scalar(db, "SELECT COUNT(*) FROM fairwitness_receipts") == 0
    assert scalar(db, "SELECT seq FROM clock") == 0

    # The agent retries with the same op_id after the crash: it commits exactly once.
    healthy = refunds.fairwitness(db)
    receipt = healthy.commit(proposal)
    assert receipt.committed and not receipt.replayed
    assert scalar(db, "SELECT COUNT(*) FROM refunds") == 1
    healthy.close()


def test_crash_while_rejecting_leaves_no_receipt(db):
    refunds.open_dispute(db, 1)

    def hook(where, _proposal):
        if where == "after_receipt":
            raise Crash(where)

    r = refunds.fairwitness(db, fault_hook=hook)
    with pytest.raises(Crash):
        r.commit(Proposal(RefundOrder(order_id=1, amount_cents=3000), "x"))
    r.close()
    assert scalar(db, "SELECT COUNT(*) FROM fairwitness_receipts") == 0


def test_connection_is_reusable_after_a_crash(db):
    calls = {"n": 0}

    def hook(where, _proposal):
        if where == "after_apply" and calls["n"] == 0:
            calls["n"] += 1
            raise Crash(where)

    r = refunds.fairwitness(db, fault_hook=hook)
    with pytest.raises(Crash):
        r.commit(Proposal(RefundOrder(order_id=1, amount_cents=1000), "a"))
    assert r.commit(Proposal(RefundOrder(order_id=2, amount_cents=1000), "b")).committed
    r.close()

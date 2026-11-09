"""Real threads racing through the executor never break the encoded invariants."""

from __future__ import annotations

import random
import threading
from pathlib import Path

from fairwitness import Proposal, refunds
from fairwitness.refunds import RefundOrder


def test_racing_threads_never_overspend_or_over_refund(tmp_path: Path):
    db = tmp_path / "race.db"
    orders = [(i, f"c{i}", 5000) for i in range(1, 11)]  # $500 of orders
    refunds.setup(db, orders, allowance_cents=20_000, policy_cap_cents=5000)  # $200 budget
    errors: list[BaseException] = []

    def agent(n: int) -> None:
        rng = random.Random(n)
        r = refunds.fairwitness(db, synchronous="OFF")
        try:
            for k in range(15):
                op = RefundOrder(
                    order_id=rng.randint(1, 10), amount_cents=rng.choice([500, 1500, 2500])
                )
                r.commit(Proposal(op, f"agent{n}:{k}"))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            r.close()

    threads = [threading.Thread(target=agent, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    v = refunds.check_invariants(db)
    assert v.total == 0, v
    conn = refunds.connect(db)
    used, limit = conn.execute("SELECT used_cents, limit_cents FROM allowances").fetchone()
    committed = conn.execute(
        "SELECT COUNT(*) FROM fairwitness_receipts WHERE status = 'committed'"
    ).fetchone()[0]
    decided = conn.execute("SELECT COUNT(*) FROM fairwitness_receipts").fetchone()[0]
    conn.close()
    assert used <= limit
    assert decided == 16 * 15
    assert 0 < committed < decided  # the budget ran out and later requests were refused

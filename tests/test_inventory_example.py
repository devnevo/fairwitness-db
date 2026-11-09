from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from examples.inventory_reservation import SCHEMA, ReserveStock
from fairwitness import FairWitness, Proposal


@pytest.mark.parametrize(
    ("qty", "reason"),
    [(0, "invalid_quantity"), (-1, "invalid_quantity"), (4, "insufficient_stock"), (2, None)],
)
def test_reservation_checks_and_effects(tmp_path: Path, qty: int, reason: str | None):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO items (sku, on_hand) VALUES ('kettle', 3)")
    conn.commit()
    conn.close()

    witness = FairWitness(path, key_columns={"items": "sku"})
    try:
        proposal = Proposal(ReserveStock("kettle", qty), op_id="cart-55:reserve")
        receipt = witness.commit(proposal)
        assert receipt.reason == reason
        assert receipt.committed == (reason is None)
        assert witness.receipt(proposal.op_id) == receipt
    finally:
        witness.close()

    conn = sqlite3.connect(path)
    try:
        expected = qty if reason is None else 0
        assert conn.execute("SELECT reserved FROM items").fetchone()[0] == expected
        assert (
            conn.execute("SELECT COALESCE(SUM(qty), 0) FROM reservations").fetchone()[0] == expected
        )
    finally:
        conn.close()

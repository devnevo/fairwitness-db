"""Define your own operation: reserve stock for an order.

The refund ledger is just the demo domain. Any write an agent proposes can be wrapped
the same way: read what it depends on, state the rules, apply the change.

    python examples/inventory_reservation.py
"""

from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from fairwitness import FairWitness, Operation, Precondition, Tx

SCHEMA = """
CREATE TABLE items (
    sku       TEXT PRIMARY KEY,
    on_hand   INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved  INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    recalled  INTEGER NOT NULL DEFAULT 0,
    version   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE reservations (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    sku   TEXT NOT NULL REFERENCES items (sku),
    qty   INTEGER NOT NULL CHECK (qty > 0)
);
"""


@dataclass(frozen=True)
class ReserveStock(Operation):
    kind: ClassVar[str] = "reserve_stock"

    sku: str
    qty: int

    def load(self, tx: Tx) -> dict[str, Any] | None:
        return tx.get("items", self.sku)

    def preconditions(self, item: dict[str, Any] | None) -> Iterable[Precondition]:
        yield Precondition("invalid_quantity", lambda: self.qty > 0, "quantity must be positive")
        yield Precondition("unknown_sku", lambda: item is not None, "item must exist")
        yield Precondition("item_recalled", lambda: not item["recalled"], "no recalled items")
        yield Precondition(
            "insufficient_stock",
            lambda: item["on_hand"] - item["reserved"] >= self.qty,
            "available stock must cover the reservation",
        )

    def apply(self, tx: Tx, item: dict[str, Any]) -> dict[str, Any]:
        rid = tx.insert("reservations", {"sku": self.sku, "qty": self.qty})
        tx.update("items", self.sku, {"reserved": item["reserved"] + self.qty})
        return {"reservation_id": rid}


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shop.db"
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.execute("INSERT INTO items (sku, on_hand) VALUES ('kettle', 3)")
        conn.commit()
        conn.close()

        r = FairWitness(path, key_columns={"items": "sku"})
        with r.snapshot() as snap:
            print("agent sees:", snap.read("items", "kettle"))
            proposal = snap.propose(ReserveStock("kettle", 2), op_id="cart-55:reserve")

        # While the agent was composing its reply, quality control recalls the item.
        conn = sqlite3.connect(path)
        conn.execute("UPDATE items SET recalled = 1, version = version + 1 WHERE sku = 'kettle'")
        conn.commit()
        conn.close()

        receipt = r.commit(proposal)
        print("decision:  ", receipt.summary())
        for d in receipt.drift:
            print(
                f"drift:      {d['table']}/{d['key']}.{d['field']}: {d['observed']} -> {d['current']}"
            )
        r.close()


if __name__ == "__main__":
    main()

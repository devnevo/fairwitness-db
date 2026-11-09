"""A small local refund ledger: the domain used by the demo, the simulation and the tests.

Tables
------
orders      one row per order; ``status`` becomes ``disputed`` when a dispute opens
refunds     one row per refund actually issued
allowances  a shared refund budget that every agent draws from
policies    refund policy (a per-refund cap) that can change while agents reason
clock       a logical clock; every write transaction takes the next ``seq``

Invariants a correct system keeps (checked by :func:`check_invariants`)
-----------------------------------------------------------------------
* no refund is issued after its order was disputed
* refunds for an order never exceed the order amount
* the shared allowance is never overspent
* no refund exceeds the policy cap in force when it was issued
* ledger columns agree with the refund rows
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from .core import FairWitness, Operation, Precondition, Tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id             INTEGER PRIMARY KEY,
    customer       TEXT    NOT NULL,
    amount_cents   INTEGER NOT NULL CHECK (amount_cents > 0),
    refunded_cents INTEGER NOT NULL DEFAULT 0 CHECK (refunded_cents >= 0),
    status         TEXT    NOT NULL DEFAULT 'paid' CHECK (status IN ('paid', 'disputed')),
    disputed_seq   INTEGER,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS refunds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id     INTEGER NOT NULL REFERENCES orders (id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    op_id        TEXT,
    seq          INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS allowances (
    name        TEXT    PRIMARY KEY,
    limit_cents INTEGER NOT NULL CHECK (limit_cents >= 0),
    used_cents  INTEGER NOT NULL DEFAULT 0,
    version     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS policies (
    name             TEXT    PRIMARY KEY,
    max_refund_cents INTEGER NOT NULL CHECK (max_refund_cents >= 0),
    version          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS policy_history (
    seq              INTEGER PRIMARY KEY,
    name             TEXT    NOT NULL,
    max_refund_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS clock (
    id  INTEGER PRIMARY KEY CHECK (id = 1),
    seq INTEGER NOT NULL
);
"""

KEY_COLUMNS = {"allowances": "name", "policies": "name"}
DEFAULT_ALLOWANCE = "refunds_daily"
DEFAULT_POLICY = "default"


# --------------------------------------------------------------------------- setup


def connect(path: str | Path, synchronous: str = "FULL") -> sqlite3.Connection:
    if synchronous.upper() not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
        raise ValueError(f"bad synchronous level: {synchronous!r}")
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA synchronous={synchronous.upper()}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def setup(
    path: str | Path,
    orders: Iterable[tuple[int, str, int]],
    *,
    allowance_cents: int,
    policy_cap_cents: int,
) -> None:
    """Create the schema and seed it. ``orders`` is ``(id, customer, amount_cents)``."""
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO clock (id, seq) VALUES (1, 0)")
        conn.executemany(
            "INSERT INTO orders (id, customer, amount_cents) VALUES (?, ?, ?)", list(orders)
        )
        conn.execute(
            "INSERT INTO allowances (name, limit_cents) VALUES (?, ?)",
            (DEFAULT_ALLOWANCE, allowance_cents),
        )
        conn.execute(
            "INSERT INTO policies (name, max_refund_cents) VALUES (?, ?)",
            (DEFAULT_POLICY, policy_cap_cents),
        )
        conn.execute(
            "INSERT INTO policy_history (seq, name, max_refund_cents) VALUES (0, ?, ?)",
            (DEFAULT_POLICY, policy_cap_cents),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def fairwitness(path: str | Path, **kwargs: Any) -> FairWitness:
    """A :class:`FairWitness` configured for this schema."""
    return FairWitness(path, key_columns=KEY_COLUMNS, **kwargs)


def tick(execute: Any) -> int:
    """Advance the logical clock inside the current write transaction."""
    execute("UPDATE clock SET seq = seq + 1 WHERE id = 1")
    return int(execute("SELECT seq FROM clock WHERE id = 1").fetchone()[0])


# --------------------------------------------------------------------------- other workers


def open_dispute(path: str | Path, order_id: int) -> None:
    """Another worker (a payments webhook, say) opens a dispute on an order."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        seq = tick(conn.execute)
        conn.execute(
            "UPDATE orders SET status = 'disputed', disputed_seq = ?, version = version + 1 "
            "WHERE id = ? AND status != 'disputed'",
            (seq, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def set_policy_cap(path: str | Path, cap_cents: int, name: str = DEFAULT_POLICY) -> None:
    """A finance admin changes the refund policy."""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        seq = tick(conn.execute)
        conn.execute(
            "UPDATE policies SET max_refund_cents = ?, version = version + 1 WHERE name = ?",
            (cap_cents, name),
        )
        conn.execute(
            "INSERT INTO policy_history (seq, name, max_refund_cents) VALUES (?, ?, ?)",
            (seq, name, cap_cents),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


# --------------------------------------------------------------------------- the operation


@dataclass
class RefundState:
    order: dict[str, Any] | None
    allowance: dict[str, Any] | None
    policy: dict[str, Any] | None


@dataclass(frozen=True)
class RefundOrder(Operation):
    """Refund part or all of an order, drawing from the shared allowance."""

    kind: ClassVar[str] = "refund_order"

    order_id: int
    amount_cents: int
    allowance: str = DEFAULT_ALLOWANCE
    policy: str = DEFAULT_POLICY

    def load(self, tx: Tx) -> RefundState:
        return RefundState(
            order=tx.get("orders", self.order_id),
            allowance=tx.get("allowances", self.allowance),
            policy=tx.get("policies", self.policy),
        )

    def preconditions(self, s: RefundState) -> Iterable[Precondition]:
        yield Precondition(
            "invalid_amount", lambda: self.amount_cents > 0, "amount must be positive"
        )
        yield Precondition("order_not_found", lambda: s.order is not None, "order must exist")
        yield Precondition(
            "order_disputed",
            lambda: s.order["status"] != "disputed",  # type: ignore[index]
            "no refunds while a dispute is open",
        )
        yield Precondition(
            "policy_missing", lambda: s.policy is not None, "refund policy must exist"
        )
        yield Precondition(
            "exceeds_policy_cap",
            lambda: self.amount_cents <= s.policy["max_refund_cents"],  # type: ignore[index]
            "amount must not exceed the current policy cap",
        )
        yield Precondition(
            "exceeds_remaining",
            lambda: (
                s.order["refunded_cents"] + self.amount_cents  # type: ignore[index]
                <= s.order["amount_cents"]
            ),  # type: ignore[index]
            "total refunds must not exceed the order amount",
        )
        yield Precondition(
            "allowance_missing", lambda: s.allowance is not None, "allowance must exist"
        )
        yield Precondition(
            "allowance_exhausted",
            lambda: (
                s.allowance["used_cents"] + self.amount_cents  # type: ignore[index]
                <= s.allowance["limit_cents"]
            ),  # type: ignore[index]
            "the shared allowance must cover the refund",
        )

    def apply(self, tx: Tx, s: RefundState) -> dict[str, Any]:
        assert s.order is not None and s.allowance is not None
        seq = tick(tx.execute)
        refund_id = tx.insert(
            "refunds",
            {"order_id": self.order_id, "amount_cents": self.amount_cents, "seq": seq},
        )
        tx.update(
            "orders",
            self.order_id,
            {"refunded_cents": s.order["refunded_cents"] + self.amount_cents},
        )
        tx.update(
            "allowances",
            self.allowance,
            {"used_cents": s.allowance["used_cents"] + self.amount_cents},
        )
        return {"refund_id": refund_id, "seq": seq}


# --------------------------------------------------------------------------- invariants


@dataclass
class Violations:
    refund_after_dispute: int = 0
    over_refunded_orders: int = 0
    allowance_overspend_cents: int = 0
    policy_cap_breaches: int = 0
    ledger_mismatches: int = 0
    wrong_cents: int = 0

    @property
    def total(self) -> int:
        return (
            self.refund_after_dispute
            + self.over_refunded_orders
            + (1 if self.allowance_overspend_cents else 0)
            + self.policy_cap_breaches
            + self.ledger_mismatches
        )


def check_invariants(path: str | Path) -> Violations:
    """Audit the ledger from the database alone."""
    conn = connect(path)
    try:
        v = Violations()
        n, cents = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(r.amount_cents), 0) FROM refunds r "
            "JOIN orders o ON o.id = r.order_id "
            "WHERE o.disputed_seq IS NOT NULL AND r.seq > o.disputed_seq"
        ).fetchone()
        v.refund_after_dispute, after_dispute_cents = n, cents

        n, excess = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(total - amount_cents), 0) FROM ("
            "  SELECT o.amount_cents, SUM(r.amount_cents) AS total FROM orders o "
            "  JOIN refunds r ON r.order_id = o.id GROUP BY o.id"
            ") WHERE total > amount_cents"
        ).fetchone()
        v.over_refunded_orders, over_cents = n, excess

        (spent, limit) = conn.execute(
            "SELECT (SELECT COALESCE(SUM(amount_cents), 0) FROM refunds), "
            "(SELECT limit_cents FROM allowances WHERE name = ?)",
            (DEFAULT_ALLOWANCE,),
        ).fetchone()
        v.allowance_overspend_cents = max(0, spent - limit)

        v.policy_cap_breaches = conn.execute(
            "SELECT COUNT(*) FROM refunds r WHERE r.amount_cents > ("
            "  SELECT h.max_refund_cents FROM policy_history h "
            "  WHERE h.seq < r.seq ORDER BY h.seq DESC LIMIT 1)"
        ).fetchone()[0]

        v.ledger_mismatches = (
            conn.execute(
                "SELECT COUNT(*) FROM orders o WHERE o.refunded_cents != "
                "(SELECT COALESCE(SUM(amount_cents), 0) FROM refunds r WHERE r.order_id = o.id)"
            ).fetchone()[0]
            + conn.execute(
                "SELECT COUNT(*) FROM allowances a WHERE a.used_cents != "
                "(SELECT COALESCE(SUM(amount_cents), 0) FROM refunds)"
            ).fetchone()[0]
        )

        # Cents that should not have left the business. Overlaps are possible across
        # categories, so this is an upper bound on a single run's damage.
        v.wrong_cents = after_dispute_cents + over_cents + v.allowance_overspend_cents
        return v
    finally:
        conn.close()

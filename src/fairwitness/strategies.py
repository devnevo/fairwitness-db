"""Five ways an agent's refund can reach the database, compared on equal terms.

Every strategy runs the same two phases:

1. ``begin(order_id)`` – the agent reads what it needs (its *context*).
2. ``finish(ticket)`` – after an arbitrary pause, the agent decides from that context
   and the strategy tries to make the change land.

Between the phases, other workers may change the world. The strategies differ only in
what happens at the moment of commitment.

==================  ==========================================================
baseline            Agent decides from its context and writes in its own
                    transaction. Correct SQL, correctly serialized. No recheck.
snapshot_review     A second reviewer (think: a second model) re-checks the
                    proposal against the *same* context, then writes.
plain_txn           Hand-written application code: re-read and check inside the
                    write transaction. The correctness baseline for FairWitness.
fairwitness         Typed proposal, preconditions re-evaluated against current
                    state, receipt committed with the effect.
fairwitness_strict  Reject when recorded row contents differ, excluding version.
==================  ==========================================================
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import refunds
from .core import Observation, Proposal, Receipt, Snapshot

STRATEGIES = ("baseline", "snapshot_review", "plain_txn", "fairwitness", "fairwitness_strict")


@dataclass
class Ticket:
    """What the agent carries from the read phase into the commit phase."""

    order_id: int
    view: dict[str, Any]
    op_id: str
    observed: tuple[Observation, ...] = ()
    snapshot: Snapshot | None = None


@dataclass
class Outcome:
    strategy: str
    order_id: int
    status: str  # "committed" | "rejected" | "skipped"
    amount_cents: int | None = None
    reason: str | None = None
    receipt: Receipt | None = None
    drift: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------------- the agent


def agent_decide(view: dict[str, Any]) -> int | None:
    """A deterministic stand-in for the model: refund what the context says is owed.

    Returns the amount to refund, or None if the context says no refund is due. It is
    deliberately reasonable. The failures in the demo come from stale context, not
    from a careless agent.
    """
    order, allowance, policy = view["order"], view["allowance"], view["policy"]
    if order is None or allowance is None or policy is None:
        return None
    if order["status"] == "disputed":
        return None
    owed = order["amount_cents"] - order["refunded_cents"]
    budget = allowance["limit_cents"] - allowance["used_cents"]
    amount = min(owed, budget, policy["max_refund_cents"])
    return amount if amount > 0 else None


def review_against_context(view: dict[str, Any], amount: int) -> bool:
    """A second check that sees only the agent's context. It agrees with the agent."""
    return agent_decide(view) is not None and amount <= (agent_decide(view) or 0)


# --------------------------------------------------------------------------- strategies


class Strategy:
    name = "strategy"

    def __init__(self, path: str | Path, *, synchronous: str = "FULL") -> None:
        self.path = str(path)
        self.synchronous = synchronous
        self._seq = 0

    def _op_id(self, order_id: int) -> str:
        self._seq += 1
        return f"{self.name}:{order_id}:{self._seq}"

    def begin(self, order_id: int) -> Ticket:
        conn = refunds.connect(self.path, self.synchronous)
        try:
            conn.execute("BEGIN")
            view = {
                "order": _row(conn, "SELECT * FROM orders WHERE id = ?", order_id),
                "allowance": _row(
                    conn, "SELECT * FROM allowances WHERE name = ?", refunds.DEFAULT_ALLOWANCE
                ),
                "policy": _row(
                    conn, "SELECT * FROM policies WHERE name = ?", refunds.DEFAULT_POLICY
                ),
            }
            conn.execute("ROLLBACK")
        finally:
            conn.close()
        return Ticket(order_id, view, self._op_id(order_id))

    def finish(self, ticket: Ticket) -> Outcome:  # pragma: no cover - interface
        raise NotImplementedError

    def close(self) -> None:
        pass


class Baseline(Strategy):
    """The agent's write is a correct, serialized transaction. Its reason is stale."""

    name = "baseline"

    def finish(self, ticket: Ticket) -> Outcome:
        amount = agent_decide(ticket.view)
        if amount is None:
            return Outcome(self.name, ticket.order_id, "skipped")
        if not self._approve(ticket, amount):
            return Outcome(self.name, ticket.order_id, "rejected", amount, "review_rejected")
        conn = refunds.connect(self.path, self.synchronous)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _write_refund(conn, ticket.order_id, amount, ticket.op_id)
            conn.execute("COMMIT")
        finally:
            conn.close()
        return Outcome(self.name, ticket.order_id, "committed", amount)

    def _approve(self, ticket: Ticket, amount: int) -> bool:
        return True


class SnapshotReview(Baseline):
    """A second opinion over the same context leaves the failure intact."""

    name = "snapshot_review"

    def _approve(self, ticket: Ticket, amount: int) -> bool:
        return review_against_context(ticket.view, amount)


class PlainTxn(Strategy):
    """The fix without a framework: put the precondition inside the write transaction."""

    name = "plain_txn"

    def finish(self, ticket: Ticket) -> Outcome:
        amount = agent_decide(ticket.view)
        if amount is None:
            return Outcome(self.name, ticket.order_id, "skipped")
        conn = refunds.connect(self.path, self.synchronous)
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = _row(conn, "SELECT * FROM orders WHERE id = ?", ticket.order_id)
            allowance = _row(
                conn, "SELECT * FROM allowances WHERE name = ?", refunds.DEFAULT_ALLOWANCE
            )
            policy = _row(conn, "SELECT * FROM policies WHERE name = ?", refunds.DEFAULT_POLICY)
            reason = None
            if order is None:
                reason = "order_not_found"
            elif order["status"] == "disputed":
                reason = "order_disputed"
            elif policy is None or amount > policy["max_refund_cents"]:
                reason = "exceeds_policy_cap"
            elif order["refunded_cents"] + amount > order["amount_cents"]:
                reason = "exceeds_remaining"
            elif allowance is None or allowance["used_cents"] + amount > allowance["limit_cents"]:
                reason = "allowance_exhausted"
            if reason is not None:
                conn.execute("ROLLBACK")
                return Outcome(self.name, ticket.order_id, "rejected", amount, reason)
            _write_refund(conn, ticket.order_id, amount, ticket.op_id)
            conn.execute("COMMIT")
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            conn.close()
        return Outcome(self.name, ticket.order_id, "committed", amount)


class FairWitnessStrategy(Strategy):
    """fairwitness-db: speculate on a snapshot, commit through the executor."""

    name = "fairwitness"
    strict = False

    def __init__(self, path: str | Path, *, synchronous: str = "FULL") -> None:
        super().__init__(path, synchronous=synchronous)
        self.fairwitness = refunds.fairwitness(path, strict=self.strict, synchronous=synchronous)

    def begin(self, order_id: int) -> Ticket:
        with self.fairwitness.snapshot() as snap:
            view = {
                "order": snap.read("orders", order_id),
                "allowance": snap.read("allowances", refunds.DEFAULT_ALLOWANCE),
                "policy": snap.read("policies", refunds.DEFAULT_POLICY),
            }
            observed = snap.observed
        return Ticket(order_id, view, self._op_id(order_id), observed)

    def finish(self, ticket: Ticket) -> Outcome:
        amount = agent_decide(ticket.view)
        if amount is None:
            return Outcome(self.name, ticket.order_id, "skipped")
        proposal = Proposal(
            refunds.RefundOrder(order_id=ticket.order_id, amount_cents=amount),
            op_id=ticket.op_id,
            observed=ticket.observed,
            agent="refund-agent",
            rationale="context shows the order is eligible and unrefunded",
        )
        receipt = self.fairwitness.commit(proposal)
        return Outcome(
            self.name,
            ticket.order_id,
            receipt.status,
            amount,
            receipt.reason,
            receipt,
            list(receipt.drift),
        )

    def close(self) -> None:
        self.fairwitness.close()


class FairWitnessStrict(FairWitnessStrategy):
    name = "fairwitness_strict"
    strict = True


def make(name: str, path: str | Path, *, synchronous: str = "FULL") -> Strategy:
    classes: dict[str, Callable[..., Strategy]] = {
        "baseline": Baseline,
        "snapshot_review": SnapshotReview,
        "plain_txn": PlainTxn,
        "fairwitness": FairWitnessStrategy,
        "fairwitness_strict": FairWitnessStrict,
    }
    try:
        return classes[name](path, synchronous=synchronous)
    except KeyError:
        raise ValueError(
            f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}"
        ) from None


# --------------------------------------------------------------------------- helpers


def _row(conn: Any, sql: str, *params: Any) -> dict[str, Any] | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else dict(row)


def _write_refund(conn: Any, order_id: int, amount: int, op_id: str) -> None:
    seq = refunds.tick(conn.execute)
    conn.execute(
        "INSERT INTO refunds (order_id, amount_cents, op_id, seq) VALUES (?, ?, ?, ?)",
        (order_id, amount, op_id, seq),
    )
    conn.execute(
        "UPDATE orders SET refunded_cents = refunded_cents + ?, version = version + 1 WHERE id = ?",
        (amount, order_id),
    )
    conn.execute(
        "UPDATE allowances SET used_cents = used_cents + ?, version = version + 1 WHERE name = ?",
        (amount, refunds.DEFAULT_ALLOWANCE),
    )

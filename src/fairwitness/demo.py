"""The demo: "The database committed the wrong refund."

Run with ``fairwitness demo`` (or ``python -m fairwitness demo``). Every scene uses a
fresh SQLite file and a fixed interleaving, so the output is the same on every run.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import refunds
from .core import IdempotencyConflict, Proposal
from .strategies import STRATEGIES, Outcome, make

ORDERS = [(1001, "ana", 8000), (1002, "ben", 4500), (1003, "chen", 12000)]
ALLOWANCE_CENTS = 50_000
POLICY_CAP_CENTS = 15_000

LABELS = {
    "baseline": "baseline (agent writes in its own txn)",
    "snapshot_review": "snapshot review (2nd check, same context)",
    "plain_txn": "plain transaction (hand-written recheck)",
    "fairwitness": "fairwitness-db",
    "fairwitness_strict": "fairwitness-db, strict mode",
}

Printer = Callable[[str], None]


def usd(cents: int | None) -> str:
    return "-" if cents is None else f"${cents / 100:,.2f}"


def fresh_db(workdir: Path, name: str) -> Path:
    path = workdir / f"{name}.db"
    refunds.setup(path, ORDERS, allowance_cents=ALLOWANCE_CENTS, policy_cap_cents=POLICY_CAP_CENTS)
    return path


def header(out: Printer, title: str) -> None:
    out("")
    out("=" * 72)
    out(title)
    out("=" * 72)


def describe(outcome: Outcome) -> str:
    if outcome.status == "committed":
        return f"COMMITTED {usd(outcome.amount_cents)}"
    if outcome.status == "rejected":
        return f"REJECTED  {outcome.reason}"
    return "SKIPPED"


# --------------------------------------------------------------------------- scenes


def scene_wrong_refund(workdir: Path, out: Printer) -> dict[str, Any]:
    header(out, "1. The database committed the wrong refund")
    out("Order #1001: $80.00, paid, nothing refunded yet.")
    out("  [agent ] reads order #1001 -> eligible, $80.00 owed")
    out("  [agent ] ...reasoning (paused)...")
    out("  [worker] payments webhook opens a dispute on order #1001")
    out("  [agent ] resumes and asks for an $80.00 refund")
    out("")
    results: dict[str, Any] = {}
    for name in STRATEGIES:
        db = fresh_db(workdir, f"s1-{name}")
        strategy = make(name, db)
        ticket = strategy.begin(1001)
        refunds.open_dispute(db, 1001)
        outcome = strategy.finish(ticket)
        strategy.close()
        audit = refunds.check_invariants(db)
        verdict = "ok" if audit.total == 0 else "WRONG: refund issued on a disputed order"
        out(f"  {LABELS[name]:<44} {describe(outcome):<30} {verdict}")
        results[name] = {"outcome": outcome, "violations": audit}
    rat = results["fairwitness"]["outcome"]
    out("")
    out("  fairwitness-db receipt (stored in the same transaction as the decision):")
    for d in rat.drift:
        out(
            f"    drift   {d['table']}/{d['key']}.{d['field']}: {d['observed']!r} -> {d['current']!r}"
        )
    for c in rat.receipt.checks:
        mark = "pass" if c["passed"] else "FAIL"
        out(f"    check   {c['reason']:<22} {mark}  ({c['rule']})")
    out("")
    out("  The baseline's transaction was correct and serialized. Its *reason* came from")
    out("  a read outside that transaction. Re-checking the same context (snapshot review)")
    out("  cannot help: it sees what the agent saw.")
    return results


def scene_both_worlds(workdir: Path, out: Printer) -> dict[str, Any]:
    header(out, "2. Two worlds, one context")
    out("World A: nothing happens while the agent reasons. World B: a dispute opens.")
    out("The agent's context is identical in both, so anything that decides from that")
    out("context alone must act the same in both. Only a check at commit time can tell")
    out("the worlds apart.")
    out("")
    out(f"  {'strategy':<44} {'world A (refund due)':<22} {'world B (disputed)':<22}")
    table: dict[str, dict[str, Any]] = {}
    for name in STRATEGIES:
        row = {}
        for world, dispute in (("A", False), ("B", True)):
            db = fresh_db(workdir, f"s2-{name}-{world}")
            strategy = make(name, db)
            ticket = strategy.begin(1001)
            if dispute:
                refunds.open_dispute(db, 1001)
            outcome = strategy.finish(ticket)
            strategy.close()
            correct = (outcome.status == "committed") != dispute
            row[world] = {"outcome": outcome, "correct": correct}
        table[name] = row
        cells = [
            f"{('ok  ' if row[w]['correct'] else 'WRONG ')}{row[w]['outcome'].status}"
            for w in ("A", "B")
        ]
        out(f"  {LABELS[name]:<44} {cells[0]:<22} {cells[1]:<22}")
    return table


def scene_policy_change(workdir: Path, out: Printer) -> dict[str, Any]:
    header(out, "3. The policy changed while the agent was thinking")
    out("Order #1003: $120.00. Policy cap: $150.00 per refund.")
    out("  [agent ] reads order and policy -> proposes a $120.00 refund")
    out("  [admin ] lowers the refund cap to $50.00")
    out("")
    results = {}
    for name in ("baseline", "fairwitness"):
        db = fresh_db(workdir, f"s3-{name}")
        strategy = make(name, db)
        ticket = strategy.begin(1003)
        refunds.set_policy_cap(db, 5_000)
        outcome = strategy.finish(ticket)
        strategy.close()
        audit = refunds.check_invariants(db)
        verdict = "ok" if audit.total == 0 else "WRONG: refund above the cap in force"
        out(f"  {LABELS[name]:<44} {describe(outcome):<30} {verdict}")
        results[name] = {"outcome": outcome, "violations": audit}
    for d in results["fairwitness"]["outcome"].drift:
        out(
            f"    drift   {d['table']}/{d['key']}.{d['field']}: {d['observed']!r} -> {d['current']!r}"
        )
    return results


def scene_irrelevant_drift(workdir: Path, out: Printer) -> dict[str, Any]:
    header(out, "4. Drift that does not matter: revalidate vs strict")
    out("Order #1002: $45.00. While the agent reasons, another agent refunds a")
    out("different order, so the shared allowance moves from $0.00 to $80.00 used.")
    out("$45.00 still fits easily in the $500.00 allowance.")
    out("")
    results = {}
    for name in ("fairwitness", "fairwitness_strict"):
        db = fresh_db(workdir, f"s4-{name}")
        strategy = make(name, db)
        ticket = strategy.begin(1002)
        other = strategy.begin(1001)
        strategy.finish(other)
        outcome = strategy.finish(ticket)
        strategy.close()
        out(f"  {LABELS[name]:<44} {describe(outcome)}")
        for d in outcome.drift:
            out(
                f"    drift   {d['table']}/{d['key']}.{d['field']}: {d['observed']!r} -> {d['current']!r}"
            )
        results[name] = outcome
    out("")
    out("  Default mode re-evaluates the encoded rules on current state and records the")
    out("  drift. Strict mode also rejects changed recorded facts that no rule mentions;")
    out("  it compares current contents, excluding version. Choose for the operation.")
    return results


def scene_retry(workdir: Path, out: Printer) -> dict[str, Any]:
    header(out, "5. The response was lost after the commit")
    db = fresh_db(workdir, "s5")
    r = refunds.fairwitness(db)
    with r.snapshot() as snap:
        snap.read("orders", 1002)
        proposal = snap.propose(
            refunds.RefundOrder(order_id=1002, amount_cents=4500),
            op_id="ticket-881:refund",
            agent="refund-agent",
        )
    first = r.commit(proposal)
    out(f"  commit  {first.summary()}  refund_id={first.effects['refund_id']}")
    out("  ...network drops the response; the agent retries the same op_id...")
    again = r.commit(proposal)
    out(f"  retry   {again.summary()}  refund_id={again.effects['refund_id']}")
    conn = refunds.connect(db)
    rows = conn.execute("SELECT COUNT(*) FROM refunds WHERE order_id = 1002").fetchone()[0]
    conn.close()
    out(f"  refunds on order #1002: {rows}")
    conflict = None
    try:
        r.commit(
            Proposal(refunds.RefundOrder(order_id=1002, amount_cents=4000), "ticket-881:refund")
        )
    except IdempotencyConflict as exc:
        conflict = exc
        out("  reuse   same op_id, different amount -> IdempotencyConflict")
    r.close()
    return {"first": first, "again": again, "refund_rows": rows, "conflict": conflict}


def run(out: Printer = print, workdir: Path | None = None) -> dict[str, Any]:
    """Run every scene. Returns the structured results (used by the tests)."""
    with tempfile.TemporaryDirectory(prefix="fairwitness-demo-") as tmp:
        wd = Path(workdir) if workdir else Path(tmp)
        results = {
            "wrong_refund": scene_wrong_refund(wd, out),
            "both_worlds": scene_both_worlds(wd, out),
            "policy_change": scene_policy_change(wd, out),
            "irrelevant_drift": scene_irrelevant_drift(wd, out),
            "retry": scene_retry(wd, out),
        }
    out("")
    out("Takeaway: the fix is a precondition inside the transaction that writes.")
    out("plain_txn proves it needs no framework. fairwitness-db makes that check declarative,")
    out("idempotent and auditable, and lets you test it with fault injection.")
    return results


def receipt_json(outcome: Outcome) -> str:
    assert outcome.receipt is not None
    return json.dumps(outcome.receipt.to_dict(), indent=2, default=str)

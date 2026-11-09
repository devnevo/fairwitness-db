"""Seeded randomized interleavings: many agents, disputes and a policy change, racing.

For each seed we generate ONE plan (orders, budget, which agent targets which order,
and the exact order in which every step runs), then replay that identical plan
against each strategy. Each step is a whole transaction, so a single-threaded replay
is a faithful concurrent history at transaction granularity, and every run is
reproducible from its seed.

    fairwitness chaos --runs 200 --seed 0
"""

from __future__ import annotations

import random
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import refunds
from .strategies import STRATEGIES, make


@dataclass(frozen=True)
class Step:
    actor: str  # "agent:<n>", "dispute:<n>", "policy"
    phase: int  # agents: 0 = read, 1 = commit
    target: int | None  # order id (agents, disputes) or new cap (policy)


@dataclass(frozen=True)
class Plan:
    seed: int
    orders: tuple[tuple[int, str, int], ...]
    allowance_cents: int
    policy_cap_cents: int
    steps: tuple[Step, ...]


def make_plan(
    seed: int,
    *,
    n_orders: int = 12,
    n_agents: int = 30,
    n_disputes: int = 5,
    policy_change: bool = True,
) -> Plan:
    rng = random.Random(seed)
    orders = tuple((1000 + i, f"customer{i}", rng.randint(20, 200) * 100) for i in range(n_orders))
    total = sum(o[2] for o in orders)
    allowance = int(total * rng.uniform(0.35, 0.65))
    cap = 15_000
    pending: dict[str, list[Step]] = {}
    for a in range(n_agents):
        oid = rng.choice(orders)[0]
        pending[f"agent:{a}"] = [Step(f"agent:{a}", 0, oid), Step(f"agent:{a}", 1, oid)]
    for d, oid in enumerate(rng.sample([o[0] for o in orders], n_disputes)):
        pending[f"dispute:{d}"] = [Step(f"dispute:{d}", 0, oid)]
    if policy_change:
        pending["policy"] = [Step("policy", 0, rng.choice([4_000, 6_000, 8_000]))]
    steps: list[Step] = []
    while pending:
        actor = rng.choice(sorted(pending))
        steps.append(pending[actor].pop(0))
        if not pending[actor]:
            del pending[actor]
    return Plan(seed, orders, allowance, cap, tuple(steps))


@dataclass
class RunResult:
    strategy: str
    seed: int
    outcomes: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    violations: refunds.Violations = field(default_factory=refunds.Violations)


def run_plan(plan: Plan, strategy_name: str, workdir: Path) -> RunResult:
    db = workdir / f"{strategy_name}-{plan.seed}.db"
    refunds.setup(
        db,
        plan.orders,
        allowance_cents=plan.allowance_cents,
        policy_cap_cents=plan.policy_cap_cents,
    )
    strategy = make(strategy_name, db, synchronous="OFF")
    result = RunResult(strategy_name, plan.seed)
    tickets: dict[str, Any] = {}
    try:
        for step in plan.steps:
            if step.actor.startswith("agent:"):
                if step.phase == 0:
                    tickets[step.actor] = strategy.begin(int(step.target or 0))
                else:
                    outcome = strategy.finish(tickets.pop(step.actor))
                    result.outcomes[outcome.status] += 1
                    if outcome.reason:
                        result.reasons[outcome.reason] += 1
            elif step.actor.startswith("dispute:"):
                refunds.open_dispute(db, int(step.target or 0))
            else:
                refunds.set_policy_cap(db, int(step.target or 0))
    finally:
        strategy.close()
    result.violations = refunds.check_invariants(db)
    return result


@dataclass
class Summary:
    strategy: str
    runs: int = 0
    runs_with_violations: int = 0
    violations: int = 0
    wrong_cents: int = 0
    committed: int = 0
    rejected: int = 0
    skipped: int = 0
    reasons: Counter = field(default_factory=Counter)
    by_kind: Counter = field(default_factory=Counter)


def simulate(
    runs: int = 100,
    seed: int = 0,
    strategies: tuple[str, ...] = STRATEGIES,
    *,
    progress: Callable[[int], None] | None = None,
    **plan_kwargs: Any,
) -> dict[str, Summary]:
    summaries = {s: Summary(s) for s in strategies}
    with tempfile.TemporaryDirectory(prefix="fairwitness-chaos-") as tmp:
        workdir = Path(tmp)
        for i in range(runs):
            plan = make_plan(seed + i, **plan_kwargs)
            for name in strategies:
                r = run_plan(plan, name, workdir)
                s = summaries[name]
                v = r.violations
                s.runs += 1
                s.violations += v.total
                s.runs_with_violations += 1 if v.total else 0
                s.wrong_cents += v.wrong_cents
                s.committed += r.outcomes["committed"]
                s.rejected += r.outcomes["rejected"]
                s.skipped += r.outcomes["skipped"]
                s.reasons.update(r.reasons)
                s.by_kind.update(
                    {
                        "refund_after_dispute": v.refund_after_dispute,
                        "over_refunded_orders": v.over_refunded_orders,
                        "allowance_overspent_runs": 1 if v.allowance_overspend_cents else 0,
                        "policy_cap_breaches": v.policy_cap_breaches,
                        "ledger_mismatches": v.ledger_mismatches,
                    }
                )
            if progress:
                progress(i + 1)
    return summaries


def render(summaries: dict[str, Summary], runs: int, seed: int) -> str:
    lines = [
        f"{runs} seeded runs (seeds {seed}..{seed + runs - 1}); each run: 12 orders, 30 agents,",
        "5 disputes and 1 policy change, interleaved at random. Same plan for every strategy.",
        "",
        f"{'strategy':<20} {'committed':>9} {'rejected':>9} {'runs w/ violations':>19} "
        f"{'violations':>11} {'excess paid*':>14}",
    ]
    for s in summaries.values():
        lines.append(
            f"{s.strategy:<20} {s.committed:>9} {s.rejected:>9} "
            f"{f'{s.runs_with_violations}/{s.runs}':>19} {s.violations:>11} "
            f"{'$' + format(s.wrong_cents / 100, ',.2f'):>14}"
        )
    lines.append(
        "* refunds after a dispute + over-refund excess + allowance overspend. Categories can"
    )
    lines.append("  overlap, so treat it as an upper bound on money that should not have moved.")
    lines.append("")
    lines.append("Violations by kind:")
    kinds = [
        "refund_after_dispute",
        "over_refunded_orders",
        "allowance_overspent_runs",
        "policy_cap_breaches",
        "ledger_mismatches",
    ]
    lines.append(f"  {'strategy':<20} " + " ".join(f"{k:>24}" for k in kinds))
    for s in summaries.values():
        lines.append(f"  {s.strategy:<20} " + " ".join(f"{s.by_kind[k]:>24}" for k in kinds))
    lines.append("")
    lines.append("Rejection reasons:")
    for s in summaries.values():
        if s.reasons:
            top = ", ".join(f"{k} {v}" for k, v in s.reasons.most_common())
            lines.append(f"  {s.strategy:<20} {top}")
    return "\n".join(lines)

"""Command line: ``fairwitness demo | chaos | receipts``."""

from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__, chaos, demo, refunds
from .strategies import STRATEGIES


def _demo(_: argparse.Namespace) -> int:
    demo.run()
    return 0


def _chaos(args: argparse.Namespace) -> int:
    strategies = tuple(args.strategies.split(",")) if args.strategies else STRATEGIES
    for s in strategies:
        if s not in STRATEGIES:
            print(f"unknown strategy {s!r}; choose from {', '.join(STRATEGIES)}", file=sys.stderr)
            return 2
    started = time.perf_counter()

    def progress(done: int) -> None:
        if sys.stderr.isatty():
            print(f"\r  run {done}/{args.runs}", end="", file=sys.stderr, flush=True)

    summaries = chaos.simulate(args.runs, args.seed, strategies, progress=progress)
    if sys.stderr.isatty():
        print("\r" + " " * 30 + "\r", end="", file=sys.stderr)
    if args.json:
        payload = {
            name: {
                "runs": s.runs,
                "runs_with_violations": s.runs_with_violations,
                "violations": s.violations,
                "wrong_cents": s.wrong_cents,
                "committed": s.committed,
                "rejected": s.rejected,
                "skipped": s.skipped,
                "reasons": dict(s.reasons),
                "by_kind": dict(s.by_kind),
            }
            for name, s in summaries.items()
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(chaos.render(summaries, args.runs, args.seed))
        print(f"\n(finished in {time.perf_counter() - started:.1f}s)")
    bad = [
        n
        for n in ("plain_txn", "fairwitness", "fairwitness_strict")
        if n in summaries and summaries[n].violations
    ]
    return 1 if bad else 0


def _receipts(args: argparse.Namespace) -> int:
    r = refunds.fairwitness(args.db)
    for receipt in r.receipts(args.limit):
        if args.json:
            print(json.dumps(receipt.to_dict(), default=str))
        else:
            print(receipt.summary())
    r.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fairwitness",
        description="Controlled database writes with current-state checks and safe retries.",
    )
    parser.add_argument("--version", action="version", version=f"fairwitness {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("demo", help='run "The database committed the wrong refund"')
    p.set_defaults(func=_demo)

    p = sub.add_parser("chaos", help="seeded randomized interleavings across strategies")
    p.add_argument("--runs", type=int, default=100)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--strategies", help=f"comma-separated subset of: {','.join(STRATEGIES)}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=_chaos)

    p = sub.add_parser("receipts", help="list decision receipts in a database")
    p.add_argument("db")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_receipts)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

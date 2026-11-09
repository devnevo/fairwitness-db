# Contributing

Thanks for looking. fairwitness-db is small on purpose: every feature should come with a test that would fail without it.

## Setup

```bash
python3 -m venv .venv && . .venv/bin/activate  # Python 3.10 or newer
python -m pip install -e '.[dev]'
make test      # pytest
make lint      # ruff check + format check
make demo      # the five scenes
make chaos     # 200 seeded runs
make build     # wheel and source distribution
```

## Ground rules

- Keep the runtime dependency count at zero.
- A new guarantee needs a test that breaks it on purpose (see `tests/test_atomicity.py` for the fault-hook pattern).
- If you change the simulation, paste before/after `fairwitness chaos --runs 200` output in the PR.
- Rejection reasons are API. Name them so an agent can act on them.

## Good first issues

1. `fairwitness receipts --op-id <id>` to print one receipt as pretty JSON.
2. Record how long each precondition took (microseconds) in the receipt's `checks`.
3. Property-based tests with Hypothesis for `canonical_json` and `fingerprint` stability.
4. Export receipts as JSON Lines for analysis (`fairwitness receipts --jsonl`).
5. Run `mypy --strict` in CI and fix what it finds.
6. A `TransferFunds` example operation with a two-row invariant (no negative balances).
7. A `--threads` mode for `chaos` that uses real threads instead of a replayed schedule.
8. Docs: a sequence diagram for the retry-after-lost-response path.

## Bigger pieces (talk first in an issue)

- MCP server (v0.2), PostgreSQL backend (v0.3), outbox and reconciliation (v0.4). See [docs/roadmap.md](docs/roadmap.md).

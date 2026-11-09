# Changelog

## 0.1.0 (unreleased)

Initial alpha implementation.

- Distribution name `fairwitness-db`, Python package `fairwitness`, console command `fairwitness`, and public executor `FairWitness`.

- `FairWitness` commit executor on SQLite (WAL, `BEGIN IMMEDIATE`): replay check, load, preconditions, apply and receipt in one transaction.
- `Snapshot` read views with recorded observations; field-level evidence drift in every receipt.
- Typed `Operation` / `Precondition` API with actionable rejection reasons.
- Idempotency by `op_id` with argument fingerprints; rejections are final per `op_id`.
- Strict mode (`stale_evidence` when recorded row contents differ, excluding `version`).
- Fault hooks at every commit stage.
- Demo domain: local refund ledger with disputes, shared allowance, refund policy and an invariant auditor.
- `fairwitness demo` (five deterministic scenes) and `fairwitness chaos` (seeded interleavings across five strategies).
- Inventory example rejects nonpositive quantities with an actionable receipt.
- 37 tests; CI configured for Python 3.10 to 3.13, including package builds and an installed-wheel smoke check.

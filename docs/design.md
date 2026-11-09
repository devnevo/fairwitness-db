# Design notes

## 1. The failure

An agent changes shared state based on what it read earlier: database rows, retrieved documents, a policy, a permission. Reasoning takes seconds to minutes. Any of those inputs can change in the meantime.

```
t0  agent reads order 1001            -> paid, $80 owed
t1  webhook opens a dispute on 1001   -> refunds now forbidden
t2  agent writes refund for 1001      -> commits
```

The write at `t2` is a perfectly good transaction. Under any isolation level, including `SERIALIZABLE`, it is serialized correctly against the dispute. The database never promised more: isolation orders the *transactions you submit*. The agent's read at `t0` was not part of the write transaction, so the database could not know the write depended on it.

## 2. Why a second look at the same context cannot help

Let `C` be everything the agent has seen. Take two worlds, `A` (no dispute) and `B` (dispute at `t1`), that produce the same `C`. Any decision procedure `d` that is a function of `C` alone returns `d(C)` in both worlds. If `d(C) = refund`, it is wrong in `B`; if `d(C) = no refund`, it is wrong in `A`.

A second model, a rule engine, or a human reviewing the same context is still a function of `C`. To separate the worlds, the decision needs an input that differs between them: a fresh, authoritative read, taken at the moment of commitment, in the same transaction as the write. The demo's "two worlds" scene and the `snapshot_review` strategy show this concretely.

## 3. Framing: ACID, not CAP

The tempting headline is a "CAP theorem for agents". CAP concerns availability versus consistency when replicas cannot communicate. An agent acting on a stale belief is a different failure: one node, fully connected, wrong because time passed. Byzantine fault tolerance helps honest participants agree despite faulty ones; they can still agree on the wrong business action. The useful analogy is a transaction's *precondition*: the classic fix is to make the facts a write depends on part of the transaction that performs it. fairwitness-db packages that fix for agent workflows.

## 4. Model

| Concept | What it is |
| --- | --- |
| **Snapshot** | A read-only SQLite transaction held open in WAL mode. Every read sees one consistent version. Reads are recorded as `Observation`s. |
| **Operation** | A frozen dataclass with `kind`, arguments and three methods: `load(tx)`, `preconditions(state)`, `apply(tx, state)`. |
| **Proposal** | An operation, an `op_id`, the observations it was based on, and optional agent/rationale metadata. Building one touches nothing. |
| **Executor** | `FairWitness.commit(proposal)`. Runs replay check, load, preconditions, apply and receipt in one `BEGIN IMMEDIATE` transaction. |
| **Receipt** | The durable decision: status, reason, every evaluated check, field-level drift, effects, timestamp. Stored with the effect. |

## 5. Semantics

**Serialization.** `BEGIN IMMEDIATE` takes SQLite's write lock at the start of commit, so executor commits are serial. `load` and `apply` run in the same transaction, so nothing `load` read can change before `apply` writes. Preconditions therefore hold in the exact state the effect is applied to.

**Atomicity.** The effect rows and the receipt row are written in one transaction. The fault hook raises at `after_load`, `after_checks`, `after_apply` and `after_receipt`; the tests assert that each injected exception leaves no effect, no receipt and no clock advance, and that a retry then commits exactly once. These exercise rollback paths; they do not simulate process termination or power loss.

**Idempotency.** `op_id` is the unit of decision.

- Same `op_id`, same arguments: the stored receipt is returned with `replayed=True`, and nothing is applied again.
- Same `op_id`, different arguments (compared by a SHA-256 fingerprint of canonical JSON): `IdempotencyConflict`.
- A **rejection is final** for its `op_id`. A lost rejection, retried, returns the same rejection. To try again after re-reading, the agent proposes under a new `op_id`. This keeps one id equal to one decision, which makes receipts a clean audit log.
- Transient failures (lock timeout, crash) roll back and leave no receipt, so retrying them is safe.
- Derive `op_id` from the agent's task (`f"{ticket}:refund"`), not from a random source, or retries after a crash will not be recognized.

**Revalidation vs strict.** Default mode re-evaluates the operation's preconditions on current state, and records drift for audit. Strict mode (`strict=True`) rejects with `stale_evidence` if an observed row's current contents differ, excluding the `version` column. It compares endpoint values, so it does not detect a value that changed and then returned to its observed value, or dependencies that were not recorded. In the 200-run simulation, strict mode committed about 40% fewer refunds, with zero encoded invariant violations in either mode. It can reject changes to recorded facts no rule captures, at the cost of additional rejections.

**Durability.** WAL mode with `synchronous=FULL` by default. The simulation uses `synchronous=OFF` for speed, which does not affect isolation or atomicity within a run.

## 6. Failure table

| Situation | Behaviour |
| --- | --- |
| Evidence changed and a rule now fails | Rejected receipt with the failing rule's reason; no effect |
| Evidence changed but every rule still holds | Committed; drift listed in the receipt (default mode) |
| Same, strict mode | Rejected with `stale_evidence` |
| Response lost after commit; agent retries | Stored receipt replayed; effect applied once |
| `op_id` reused with different arguments | `IdempotencyConflict`; nothing written |
| Exception inside the commit transaction | Full rollback; no receipt; retry is safe |
| Two agents commit the same `op_id` concurrently | One decides, the other replays (tested with 12 threads) |
| 16 threads drain a shared budget | Budget never overspent; ledger consistent (tested) |
| Write bypasses the executor | Not covered. Use database constraints or triggers where possible; SQLite file access does not enforce a per-caller FairWitness boundary |
| External system (payment provider) | Not covered in v1. Needs an outbox and reconciliation |

## 7. Decisions and alternatives

- **Record rejections instead of raising.** A rejection is a normal outcome the agent should act on. Recording it makes retries deterministic and gives a full audit trail of what agents attempted.
- **Revalidate by default, strict by choice.** Version checks (classic optimistic concurrency) reject on irrelevant changes, such as another refund moving the shared allowance. Semantic revalidation commits those safely. The simulation quantifies the trade-off rather than asserting it.
- **One local transaction, no two-phase commit.** Everything v1 touches lives in one database, so a single transaction is both simpler and stronger.
- **SQLite for v1.** Zero setup, a real single-writer serialization point, and WAL snapshots. PostgreSQL is next, where the interesting work is predicate preconditions under `SERIALIZABLE`.
- **The honest comparison is a plain transaction.** `plain_txn` implements the same rules by hand, and the simulation shows fairwitness-db makes identical decisions. fairwitness-db's value is that the check is declared once, named, idempotent, recorded with drift, and tested with injected failures.

## 8. Inspiration, kept in its place

Hopfield's kinetic proofreading (1974) shows how biological systems spend extra energy on a second, delayed check before an irreversible step, lowering error rates beyond what a single binding step allows. "Check again, at the last moment, against the real thing" is the same shape. The operative mechanism here is ordinary revalidation against authoritative state inside a transaction; the analogy is inspiration, not a claim.

## 9. Open questions

- How should preconditions over *sets* be expressed and enforced (no open dispute for any of this customer's orders)? Under PostgreSQL `SERIALIZABLE` the database can detect the conflict; in SQLite the single writer makes it trivial.
- What is the right retry policy for an agent after a rejection: re-read and re-plan, escalate, or give up? Receipts provide the signal; the policy belongs to the agent.
- Can the agent's reads be captured automatically (through a tool layer such as MCP) so the observed set is complete without manual `snap.read` calls?

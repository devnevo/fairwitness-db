# Roadmap

Each version ships something runnable, with a measurable claim and tests that back it.

| Version | Adds | Exit criteria |
| --- | --- | --- |
| **v0.1** (implemented) | SQLite executor, typed operations, receipts, idempotency, drift, strict mode, demo, chaos simulation | 0 encoded invariant violations in 200 seeded runs; injected exceptions at four commit stages leave no partial state |
| **v0.2** | MCP server exposing `snapshot_read`, `propose`, `commit`, `receipt`; a real LLM agent in the demo loop; JSON-schema tool specs generated from operations | An LLM agent driven through MCP reproduces the wrong-refund failure without fairwitness and is stopped with it; recorded demo video |
| **v0.3** | PostgreSQL backend under `SERIALIZABLE`; predicate preconditions over sets of rows; serialization-failure retries; benchmark of commit overhead | Same chaos suite passes on Postgres; overhead per commit reported with p50/p99 |
| **v0.4** | External effects: transactional outbox, provider idempotency keys, reconciliation against a mock payment provider with injected faults | No double charge and no lost refund under injected timeouts, duplicates and out-of-order webhooks |
| **v0.5** | Policy as data: versioned policies, approval thresholds (human-in-the-loop above $X), per-agent authority | Receipts show which policy version and which authority admitted each effect |

Good first issues live in [CONTRIBUTING.md](../CONTRIBUTING.md).

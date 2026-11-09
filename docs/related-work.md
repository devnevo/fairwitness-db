# Related work

fairwitness-db implements an established idea in a small, reproducible form. These are the closest neighbours.

## Agent transactions (2026)

- **When AI Agents Commit: Cognitive Serializability Across Data, Evidence, Policy, and Authority.** Jun He, Deying Yu. arXiv:2609.20261. <https://arxiv.org/abs/2609.20261>
  The closest work. It argues that database isolation only orders the submitted transaction, introduces typed dependency tokens, and defines a weaker admission mode that rechecks an effect against current dependencies and current policy at commit time, with receipts committed alongside the effect. fairwitness-db applies the revalidation idea to single-node SQL. Its strict mode compares recorded row contents; it does not implement the paper's complete dependency, authority or fencing protocol.

- **Agentic Transaction: Towards ACID-Compliant Agent Systems.** Zhaoyan Sun, Xiaoxiao Wang, Guoliang Li. arXiv:2608.13900. <https://arxiv.org/abs/2608.13900>
  Adapts atomicity, consistency, isolation and durability to multi-step agent execution, with explore-execute-validate cycles and dependency-aware isolation. Broader in scope (whole agent runs) than fairwitness-db's single commit point.

## Database foundations

- **PostgreSQL: Transaction Isolation.** <https://www.postgresql.org/docs/16/transaction-iso.html>
  What isolation levels do and do not promise. Isolation orders submitted transactions; it cannot see reads made outside them.
- **SQLite: Atomic Commit.** <https://www.sqlite.org/atomiccommit.html>
  How SQLite makes a transaction all-or-nothing, which fairwitness-db relies on to commit effect and receipt together.
- **H. T. Kung and J. T. Robinson, "On Optimistic Methods for Concurrency Control," ACM TODS, 1981.**
  The validate-then-write pattern behind strict mode.
- **M. Kleppmann and C. Riccomini, *Designing Data-Intensive Applications*, 2nd ed., O'Reilly, 2026.** <https://www.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/>
  Transactions, write skew and the "decide on a stale read" family of anomalies.

## Idempotency

- **Brandur Leach, "Implementing Stripe-like Idempotency Keys in Postgres."** <https://brandur.org/idempotency-keys>
  Idempotency keys stored transactionally with the work they protect.
- **Pat Helland, "Idempotence Is Not a Medical Condition," ACM Queue, 2012.** <https://queue.acm.org/detail.cfm?id=2187821>

## Framing

- **S. Gilbert and N. Lynch, "Brewer's Conjecture and the Feasibility of Consistent, Available, Partition-Tolerant Web Services," 2002.** <https://www.cs.princeton.edu/courses/archive/spr22/cos418/papers/cap.pdf>
  Why CAP is the wrong headline: it concerns partitioned replicas, not stale beliefs.
- **L. Lamport, R. Shostak, M. Pease, "The Byzantine Generals Problem," 1982.** <https://www.cs.cmu.edu/afs/cs/academic/class/15712-f08/www/readings/lamport82.pdf>
  Agreement despite faulty participants. Participants can still agree on the wrong action.

## Inspiration

- **J. J. Hopfield, "Kinetic proofreading: a new mechanism for reducing errors in biosynthetic processes requiring high specificity," PNAS, 1974.** <https://pubmed.ncbi.nlm.nih.gov/4530290/>
  A delayed second check before an irreversible step. Inspiration only (see design notes, section 8).

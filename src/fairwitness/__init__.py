"""fairwitness-db: controlled database writes for AI agents and applications.

Speculate freely on a snapshot; commit only against current evidence.

    from fairwitness import FairWitness, Operation, Precondition

See README.md for the model and docs/design.md for the guarantees.
"""

from .core import (
    FairWitness,
    FairWitnessError,
    IdempotencyConflict,
    Observation,
    Operation,
    Precondition,
    Proposal,
    Receipt,
    Snapshot,
    Tx,
    canonical_json,
    fingerprint,
    new_op_id,
)

__version__ = "0.1.0"

__all__ = [
    "IdempotencyConflict",
    "Observation",
    "Operation",
    "Precondition",
    "Proposal",
    "FairWitness",
    "FairWitnessError",
    "Receipt",
    "Snapshot",
    "Tx",
    "canonical_json",
    "fingerprint",
    "new_op_id",
    "__version__",
]

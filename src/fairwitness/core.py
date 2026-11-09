"""fairwitness-db core: speculate freely, commit only against current evidence.

The agent works against a *snapshot*: a consistent, read-only view of the
database. It can read, reason, propose, revise and discard as much as it likes.
Nothing it does there touches shared state.

When the agent wants a change to land, it hands a typed *proposal* to
:meth:`FairWitness.commit`. The executor then, inside ONE write transaction:

1. returns the stored receipt if this ``op_id`` was already decided (idempotent retry),
2. reloads the authoritative state the operation depends on,
3. re-evaluates the operation's preconditions against that *current* state,
4. applies the effect only if every precondition still holds, and
5. stores a receipt (committed or rejected, with reasons and evidence drift)
   in the same transaction as the effect.

So either the effect and its receipt both exist, or neither does.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

__all__ = [
    "Observation",
    "Operation",
    "Precondition",
    "Proposal",
    "FairWitness",
    "FairWitnessError",
    "IdempotencyConflict",
    "Receipt",
    "Snapshot",
    "Tx",
    "canonical_json",
    "fingerprint",
    "new_op_id",
]

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

RECEIPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS fairwitness_receipts (
    op_id       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    reason      TEXT,
    body        TEXT NOT NULL,
    decided_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS fairwitness_receipts_decided_at ON fairwitness_receipts (decided_at);
"""

FAULT_POINTS = ("after_load", "after_checks", "after_apply", "after_receipt")


# --------------------------------------------------------------------------- helpers


def _ident(name: str) -> str:
    """Table and column names are interpolated into SQL, so only allow plain identifiers."""
    if not isinstance(name, str) or not _IDENT.match(name):
        raise ValueError(f"not a safe SQL identifier: {name!r}")
    return name


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace. Used for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(kind: str, args: Mapping[str, Any]) -> str:
    """Stable hash of an operation's kind and arguments."""
    return hashlib.sha256(canonical_json({"kind": kind, "args": dict(args)}).encode()).hexdigest()


def new_op_id(prefix: str = "op") -> str:
    """A random operation id.

    Prefer deriving op ids from the agent's task (for example ``f"{task_id}:refund"``)
    so a retry after a crash reuses the same id. A fresh random id per call defeats
    idempotency.
    """
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def _obs_key(table: str, key: Any) -> tuple[str, str]:
    return (table, canonical_json(key))


# --------------------------------------------------------------------------- errors


class FairWitnessError(Exception):
    """Base class for fairwitness-db errors."""


class IdempotencyConflict(FairWitnessError):
    """An ``op_id`` that was already decided was reused with different arguments."""

    def __init__(self, op_id: str, stored: str, attempted: str) -> None:
        super().__init__(
            f"op_id {op_id!r} was already decided for a different operation "
            f"(stored fingerprint {stored[:12]}…, attempted {attempted[:12]}…)"
        )
        self.op_id = op_id
        self.stored_fingerprint = stored
        self.attempted_fingerprint = attempted


# --------------------------------------------------------------------------- value types


@dataclass(frozen=True)
class Precondition:
    """A named rule that must hold in the *current* state for an operation to commit.

    ``reason`` is returned to the agent when the rule fails, so make it something the
    agent can act on (``order_disputed``, not ``check_3_failed``). ``holds`` is a
    zero-argument callable, evaluated lazily and in order; evaluation stops at the
    first failure, so later rules may assume earlier ones passed.
    """

    reason: str
    holds: Callable[[], bool]
    rule: str = ""


@dataclass(frozen=True)
class Observation:
    """One row as seen at some moment (``row is None`` means the row did not exist)."""

    table: str
    key: Any
    row: dict[str, Any] | None

    @property
    def version(self) -> Any:
        return None if self.row is None else self.row.get("version")


class Operation:
    """Base class for typed operations. Subclass as a frozen dataclass.

    Implement three methods:

    * ``load(tx)`` – read the authoritative state this operation depends on.
    * ``preconditions(state)`` – yield :class:`Precondition` rules over that state.
    * ``apply(tx, state)`` – perform the writes; return a small dict describing the effect.

    ``load`` and ``apply`` run inside the commit transaction, so what ``load`` reads
    cannot change before ``apply`` writes.
    """

    kind: ClassVar[str] = ""

    def args(self) -> dict[str, Any]:
        if not dataclasses.is_dataclass(self):
            raise TypeError(f"{type(self).__name__} must be a dataclass")
        return dataclasses.asdict(self)

    def load(self, tx: Tx) -> Any:  # pragma: no cover - interface
        raise NotImplementedError

    def preconditions(self, state: Any) -> Iterable[Precondition]:  # pragma: no cover
        raise NotImplementedError

    def apply(self, tx: Tx, state: Any) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


@dataclass(frozen=True)
class Proposal:
    """A typed operation plus the evidence the agent based it on."""

    op: Operation
    op_id: str
    observed: tuple[Observation, ...] = ()
    agent: str | None = None
    rationale: str | None = None


@dataclass(frozen=True)
class Receipt:
    """The durable record of a decision. Stored in the same transaction as the effect."""

    op_id: str
    kind: str
    args: dict[str, Any]
    fingerprint: str
    status: str  # "committed" | "rejected"
    reason: str | None
    checks: tuple[dict[str, Any], ...]
    drift: tuple[dict[str, Any], ...]
    effects: dict[str, Any] | None
    agent: str | None
    rationale: str | None
    decided_at: float
    replayed: bool = False

    @property
    def committed(self) -> bool:
        return self.status == "committed"

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["checks"] = list(self.checks)
        data["drift"] = list(self.drift)
        return data

    @classmethod
    def from_body(cls, body: str, *, replayed: bool = False) -> Receipt:
        data = json.loads(body)
        data["checks"] = tuple(data.get("checks") or ())
        data["drift"] = tuple(data.get("drift") or ())
        data["replayed"] = replayed
        return cls(**data)

    def summary(self) -> str:
        head = f"{self.op_id} {self.kind} {self.status.upper()}"
        if self.reason:
            head += f" ({self.reason})"
        if self.replayed:
            head += " [replayed]"
        return head


# --------------------------------------------------------------------------- transaction view


class Tx:
    """Reads and writes inside the commit transaction. Every read is recorded."""

    def __init__(self, conn: sqlite3.Connection, fairwitness: FairWitness) -> None:
        self._conn = conn
        self._fairwitness = fairwitness
        self.reads: dict[tuple[str, str], Observation] = {}
        self.writes: list[dict[str, Any]] = []

    def get(self, table: str, key: Any) -> dict[str, Any] | None:
        row = self._fairwitness._select(self._conn, table, key)
        self.reads[_obs_key(table, key)] = Observation(table, key, row)
        return None if row is None else dict(row)

    def update(self, table: str, key: Any, changes: Mapping[str, Any]) -> None:
        if not changes:
            raise ValueError("update needs at least one column")
        t = _ident(table)
        k = self._fairwitness.key_column(table)
        cols = [_ident(c) for c in changes]
        sets = ", ".join(f"{c} = ?" for c in cols)
        if "version" in self._fairwitness.columns(self._conn, table) and "version" not in changes:
            sets += ", version = version + 1"
        cur = self._conn.execute(f"UPDATE {t} SET {sets} WHERE {k} = ?", (*changes.values(), key))
        if cur.rowcount != 1:
            raise FairWitnessError(f"update of {table}/{key!r} matched {cur.rowcount} rows")
        self.writes.append({"op": "update", "table": table, "key": key, "set": dict(changes)})

    def insert(self, table: str, row: Mapping[str, Any]) -> int:
        t = _ident(table)
        cols = [_ident(c) for c in row]
        marks = ", ".join("?" for _ in cols)
        cur = self._conn.execute(
            f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({marks})", tuple(row.values())
        )
        rowid = int(cur.lastrowid or 0)
        self.writes.append({"op": "insert", "table": table, "key": rowid, "row": dict(row)})
        return rowid

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        """Escape hatch for statements fairwitness does not model. Not recorded in the receipt."""
        return self._conn.execute(sql, tuple(params))


# --------------------------------------------------------------------------- snapshot


class Snapshot:
    """A consistent read-only view for the agent to reason over.

    Holds one read transaction open (SQLite WAL mode), so every read sees the database
    as of the first read, even while other writers commit. Each read is recorded as an
    :class:`Observation`; proposals carry them so the commit step can report exactly
    which facts changed while the agent was thinking.
    """

    def __init__(self, fairwitness: FairWitness) -> None:
        self._fairwitness = fairwitness
        self._conn: sqlite3.Connection | None = fairwitness._new_connection()
        self._conn.execute("BEGIN")
        self._obs: dict[tuple[str, str], Observation] = {}

    def read(self, table: str, key: Any) -> dict[str, Any] | None:
        if self._conn is None:
            raise FairWitnessError("snapshot is closed")
        row = self._fairwitness._select(self._conn, table, key)
        self._obs[_obs_key(table, key)] = Observation(table, key, row)
        return None if row is None else dict(row)

    @property
    def observed(self) -> tuple[Observation, ...]:
        return tuple(self._obs.values())

    def propose(
        self,
        op: Operation,
        *,
        op_id: str | None = None,
        agent: str | None = None,
        rationale: str | None = None,
    ) -> Proposal:
        """Package an operation with this snapshot's evidence. Touches nothing."""
        return Proposal(op, op_id or new_op_id(), self.observed, agent, rationale)

    def close(self) -> None:
        """End the read transaction. Observations stay available for proposals."""
        if self._conn is not None:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Snapshot:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# --------------------------------------------------------------------------- executor


class FairWitness:
    """The commit executor for one SQLite database file.

    Parameters
    ----------
    path:
        SQLite database file. (``:memory:`` is not supported: snapshots and commits
        use separate connections.)
    strict:
        If True, reject with ``stale_evidence`` when an observed row's current contents
        differ from the snapshot, excluding the ``version`` column, even if every
        encoded precondition still holds. This compares endpoint values, not change
        history. If False (default), preconditions are re-evaluated against the current
        state and drift is recorded in the receipt for audit.
    key_columns:
        Primary-key column per table, for tables whose key is not ``id``.
    synchronous:
        SQLite ``PRAGMA synchronous`` level. ``FULL`` (default) is durable on power loss.
    fault_hook:
        Test hook called as ``fault_hook(point, proposal)`` at each of
        :data:`FAULT_POINTS`. Raise inside it to inject a failure mid-commit.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        strict: bool = False,
        key_columns: Mapping[str, str] | None = None,
        synchronous: str = "FULL",
        fault_hook: Callable[[str, Proposal], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if str(path) == ":memory:":
            raise ValueError("fairwitness-db needs a database file, not :memory:")
        if synchronous.upper() not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError(f"bad synchronous level: {synchronous!r}")
        self.path = str(path)
        self.strict = strict
        self.synchronous = synchronous.upper()
        self.fault_hook = fault_hook
        self.clock = clock
        self._key_columns = {_ident(t): _ident(c) for t, c in (key_columns or {}).items()}
        self._columns: dict[str, frozenset[str]] = {}
        self._local = threading.local()
        conn = self._new_connection()
        try:
            conn.executescript(RECEIPTS_SCHEMA)
        finally:
            conn.close()

    # -- connections -------------------------------------------------------

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, isolation_level=None, timeout=30.0, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA synchronous={self.synchronous}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _connection(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close this thread's cached connection."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- schema helpers ----------------------------------------------------

    def key_column(self, table: str) -> str:
        return self._key_columns.get(_ident(table), "id")

    def columns(self, conn: sqlite3.Connection, table: str) -> frozenset[str]:
        t = _ident(table)
        cols = self._columns.get(t)
        if cols is None:
            cols = frozenset(r["name"] for r in conn.execute(f"PRAGMA table_info({t})"))
            if not cols:
                raise FairWitnessError(f"no such table: {table}")
            self._columns[t] = cols
        return cols

    def _select(self, conn: sqlite3.Connection, table: str, key: Any) -> dict[str, Any] | None:
        t = _ident(table)
        k = self.key_column(table)
        self.columns(conn, table)  # raises on unknown table
        row = conn.execute(f"SELECT * FROM {t} WHERE {k} = ?", (key,)).fetchone()
        return None if row is None else dict(row)

    # -- public API --------------------------------------------------------

    def snapshot(self) -> Snapshot:
        """Open a consistent read-only view for the agent to reason over."""
        return Snapshot(self)

    def receipt(self, op_id: str) -> Receipt | None:
        row = (
            self._connection()
            .execute("SELECT body FROM fairwitness_receipts WHERE op_id = ?", (op_id,))
            .fetchone()
        )
        return None if row is None else Receipt.from_body(row["body"])

    def receipts(self, limit: int = 50) -> list[Receipt]:
        rows = (
            self._connection()
            .execute(
                "SELECT body FROM fairwitness_receipts ORDER BY decided_at DESC, rowid DESC LIMIT ?",
                (limit,),
            )
            .fetchall()
        )
        return [Receipt.from_body(r["body"]) for r in rows]

    def commit(self, proposal: Proposal) -> Receipt:
        """Decide a proposal against current state, atomically with its receipt.

        Returns a committed or rejected :class:`Receipt`. A rejection is a normal,
        recorded outcome, not an exception: the agent gets a reason it can act on.
        Retrying the same ``op_id`` with the same arguments returns the stored receipt
        with ``replayed=True``; with different arguments it raises
        :class:`IdempotencyConflict`. A rejection is final for that ``op_id``: to try
        again after re-reading, propose under a new ``op_id``.
        """
        op = proposal.op
        if not op.kind:
            raise FairWitnessError(f"{type(op).__name__} has no kind")
        args = op.args()
        fp = fingerprint(op.kind, args)
        conn = self._connection()
        conn.execute("BEGIN IMMEDIATE")
        try:
            stored = conn.execute(
                "SELECT fingerprint, body FROM fairwitness_receipts WHERE op_id = ?",
                (proposal.op_id,),
            ).fetchone()
            if stored is not None:
                conn.execute("ROLLBACK")
                if stored["fingerprint"] != fp:
                    raise IdempotencyConflict(proposal.op_id, stored["fingerprint"], fp)
                return Receipt.from_body(stored["body"], replayed=True)

            tx = Tx(conn, self)
            state = op.load(tx)
            self._fault("after_load", proposal)
            drift = self._drift(proposal.observed, tx)

            checks: list[dict[str, Any]] = []
            reason: str | None = None
            if self.strict and drift:
                reason = "stale_evidence"
            else:
                for pre in op.preconditions(state):
                    passed = bool(pre.holds())
                    checks.append({"reason": pre.reason, "passed": passed, "rule": pre.rule})
                    if not passed:
                        reason = pre.reason
                        break
            self._fault("after_checks", proposal)

            effects: dict[str, Any] | None = None
            if reason is None:
                effects = dict(op.apply(tx, state) or {})
                effects.setdefault("writes", tx.writes)
                self._fault("after_apply", proposal)

            receipt = Receipt(
                op_id=proposal.op_id,
                kind=op.kind,
                args=args,
                fingerprint=fp,
                status="committed" if reason is None else "rejected",
                reason=reason,
                checks=tuple(checks),
                drift=tuple(drift),
                effects=effects,
                agent=proposal.agent,
                rationale=proposal.rationale,
                decided_at=self.clock(),
            )
            body = canonical_json({k: v for k, v in receipt.to_dict().items() if k != "replayed"})
            conn.execute(
                "INSERT INTO fairwitness_receipts "
                "(op_id, kind, fingerprint, status, reason, body, decided_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (receipt.op_id, receipt.kind, fp, receipt.status, reason, body, receipt.decided_at),
            )
            self._fault("after_receipt", proposal)
            conn.execute("COMMIT")
            return receipt
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    # -- internals ---------------------------------------------------------

    def _fault(self, point: str, proposal: Proposal) -> None:
        if self.fault_hook is not None:
            self.fault_hook(point, proposal)

    def _drift(self, observed: Iterable[Observation], tx: Tx) -> list[dict[str, Any]]:
        """Field-level differences between what the agent saw and what is true now."""
        out: list[dict[str, Any]] = []
        for obs in observed:
            current = tx.reads.get(_obs_key(obs.table, obs.key))
            now = current.row if current is not None else tx.get(obs.table, obs.key)
            then = obs.row
            if then is None and now is None:
                continue
            if then is None or now is None:
                out.append(
                    {
                        "table": obs.table,
                        "key": obs.key,
                        "field": "*",
                        "observed": "absent" if then is None else "present",
                        "current": "absent" if now is None else "present",
                    }
                )
                continue
            for name in sorted(set(then) | set(now)):
                if name == "version":
                    continue
                if then.get(name) != now.get(name):
                    out.append(
                        {
                            "table": obs.table,
                            "key": obs.key,
                            "field": name,
                            "observed": then.get(name),
                            "current": now.get(name),
                        }
                    )
        return out

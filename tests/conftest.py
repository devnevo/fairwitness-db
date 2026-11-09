from __future__ import annotations

from pathlib import Path

import pytest

from fairwitness import refunds

ORDERS = [(1, "ana", 8000), (2, "ben", 4500), (3, "chen", 12000)]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    refunds.setup(path, ORDERS, allowance_cents=20_000, policy_cap_cents=10_000)
    return path


@pytest.fixture
def fairwitness(db: Path):
    r = refunds.fairwitness(db)
    yield r
    r.close()


def scalar(db: Path, sql: str, *params):
    conn = refunds.connect(db)
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()

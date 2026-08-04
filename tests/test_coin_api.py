# SPDX-License-Identifier: AGPL-3.0-only
"""Contract tests for the per-miner coin override endpoint.

Model-level, in the repo's existing TestClient-free style: validation of
``POST /api/miners/{id}/coin`` and the round-trip through the DB helper,
plus the migration that adds the column to an existing database.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from backend import coin  # noqa: E402
from backend import db  # noqa: E402
from backend.main import CoinOverridePayload  # noqa: E402


# ---- CoinOverridePayload: what the UI may send ----

def test_accepts_supported_coins() -> None:
    assert CoinOverridePayload(coin="btc").coin == "btc"
    assert CoinOverridePayload(coin="bch").coin == "bch"


def test_normalizes_case_and_whitespace() -> None:
    assert CoinOverridePayload(coin=" BTC ").coin == "btc"


def test_null_clears_the_override() -> None:
    # Explicit null and an omitted field both mean "go back to detection".
    assert CoinOverridePayload(coin=None).coin is None
    assert CoinOverridePayload().coin is None


@pytest.mark.parametrize(
    "bad",
    [
        {"coin": "doge"},        # unsupported chain
        {"coin": "bitcoin"},     # a label, not the id
        {"coin": ""},            # blank is not the same as null
        {"coin": 42},            # wrong type
        {"coin": "btc", "unexpected": 1},   # extra="forbid"
    ],
)
def test_invalid_payloads_rejected(bad: dict) -> None:
    with pytest.raises(ValidationError):
        CoinOverridePayload(**bad)


# ---- DB round-trip + migration ----

def _make_db(tmp_path, monkeypatch):
    """Point the db module at a throwaway file and initialise the schema."""
    path = tmp_path / "minerwatch.db"
    monkeypatch.setattr(db, "db_path", lambda: path)
    asyncio.run(db.init_db())
    return path


def test_override_round_trips_and_clears(tmp_path, monkeypatch) -> None:
    _make_db(tmp_path, monkeypatch)

    async def scenario():
        miner_id = await db.upsert_miner(
            {"name": "Avalon", "family": "canaan", "host": "10.0.0.9"}
        )
        # Fresh miners start on auto-detection.
        assert (await db.get_miner(miner_id))["coin_override"] is None

        await db.set_coin_override(miner_id, "bch")
        assert (await db.get_miner(miner_id))["coin_override"] == "bch"

        await db.set_coin_override(miner_id, None)
        assert (await db.get_miner(miner_id))["coin_override"] is None
        return miner_id

    assert asyncio.run(scenario()) > 0


def test_stored_override_drives_classification(tmp_path, monkeypatch) -> None:
    """The column is what coin.classify reads — check the two line up."""
    _make_db(tmp_path, monkeypatch)

    async def scenario():
        miner_id = await db.upsert_miner(
            {"name": "Avalon", "family": "canaan", "host": "10.0.0.9"}
        )
        await db.set_coin_override(miner_id, "bch")
        return await db.get_miner(miner_id)

    row = asyncio.run(scenario())
    # No sample at all: the override alone must still classify the miner.
    assert coin.classify(row, None, {"btc": 1.2e14, "bch": 4.0e11}) == (
        "bch",
        coin.SOURCE_OVERRIDE,
    )


def test_migration_is_idempotent_on_an_existing_db(tmp_path, monkeypatch) -> None:
    # init_db runs ALTER TABLE ... ADD COLUMN unconditionally and swallows
    # the "duplicate column" error; running it twice must stay clean.
    _make_db(tmp_path, monkeypatch)
    asyncio.run(db.init_db())

    async def scenario():
        miner_id = await db.upsert_miner(
            {"name": "Rig", "family": "bitaxe", "host": "10.0.0.5"}
        )
        await db.set_coin_override(miner_id, "btc")
        return await db.get_miner(miner_id)

    assert asyncio.run(scenario())["coin_override"] == "btc"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

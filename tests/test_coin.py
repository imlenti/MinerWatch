# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for per-miner coin detection (backend/coin.py).

Covers the contract the odds, the Halo gauge and the block-found check all
depend on:

  * an explicit override always wins, even over a live stratum reading and
    even when the miner is offline;
  * a reported network difficulty is matched to the nearest coin in log
    space, and a value close to neither is rejected rather than forced;
  * pool payout addresses classify only when the format names its chain
    (``bitcoincash:`` / ``bc1``), never for legacy addresses valid on both;
  * hostname matching never reads "bitcoincash" as "bitcoin";
  * anything unresolved comes back as ``None`` — the callers rely on that
    to keep unclassified hashrate out of per-coin maths.

All fixtures below are fictional. Runs under pytest, or standalone:
``python tests/test_coin.py``.
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import coin  # noqa: E402

# Stand-in reference difficulties, ~3 orders of magnitude apart like the
# real chains. Exact values don't matter — the matcher works on ratios.
REFS = {"btc": 1.2e14, "bch": 4.0e11}


def _sample(**overrides):
    """A live MinerSample stand-in with the fields the classifier reads."""
    defaults = dict(
        online=True,
        network_difficulty=None,
        pool_url=None,
        worker=None,
        pools=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _pool(url=None, user=None, active=None):
    return SimpleNamespace(url=url, user=user, active=active)


# ---------- normalize ----------

def test_normalize_accepts_known_coins_case_insensitively():
    assert coin.normalize("BTC") == "btc"
    assert coin.normalize(" bch ") == "bch"


def test_normalize_rejects_anything_else():
    for value in (None, "", "doge", "bitcoin", 42, "btc "):
        assert coin.normalize(value) in (None, "btc")
    assert coin.normalize("doge") is None
    assert coin.normalize("") is None
    assert coin.normalize(None) is None


# ---------- difficulty matching ----------

def test_difficulty_matches_nearest_coin():
    assert coin.classify_by_difficulty(1.19e14, REFS) == "btc"
    assert coin.classify_by_difficulty(4.1e11, REFS) == "bch"


def test_difficulty_tolerates_a_retarget_sized_drift():
    # A miner still advertising the previous epoch is a couple of percent
    # off, and even a large swing must not flip it to the other chain.
    assert coin.classify_by_difficulty(1.2e14 * 1.04, REFS) == "btc"
    assert coin.classify_by_difficulty(4.0e11 * 0.6, REFS) == "bch"


def test_difficulty_far_from_every_reference_is_rejected():
    # Six orders of magnitude below BCH: a third chain or a parse error,
    # not something to guess about.
    assert coin.classify_by_difficulty(5.0e5, REFS) is None


def test_difficulty_ignores_unusable_values_and_missing_references():
    assert coin.classify_by_difficulty(None, REFS) is None
    assert coin.classify_by_difficulty(0, REFS) is None
    assert coin.classify_by_difficulty(-1, REFS) is None
    assert coin.classify_by_difficulty("n/a", REFS) is None
    assert coin.classify_by_difficulty(float("inf"), REFS) is None
    # A cold cache for one coin must not stop the other from matching.
    assert coin.classify_by_difficulty(1.2e14, {"btc": 1.2e14, "bch": None}) == "btc"
    assert coin.classify_by_difficulty(1.2e14, {"btc": None, "bch": None}) is None


# ---------- payout address ----------

def test_address_prefixes_that_name_their_chain():
    assert coin.classify_by_address("bitcoincash:qr9abc.worker") == "bch"
    assert coin.classify_by_address("bc1qexampleaddress.bitaxe") == "btc"


def test_legacy_and_bare_addresses_are_not_classified():
    # Valid on both chains, or not anchored to a chain name.
    assert coin.classify_by_address("1BoatSLRHtKNngkdXEeobR76b53LETtpyT") is None
    assert coin.classify_by_address("3FZbgi29cpjq2GjdwV8eyHuJJnkLtktZc5") is None
    assert coin.classify_by_address("qr9abcdefghijklmnop") is None
    assert coin.classify_by_address(None) is None
    assert coin.classify_by_address("") is None


# ---------- pool hostname ----------

def test_hostname_tokens():
    assert coin.classify_by_pool_url("stratum+tcp://bch.example.org:3333") == "bch"
    assert coin.classify_by_pool_url("btc-solo.example.org:3333") == "btc"
    assert coin.classify_by_pool_url("solo.bitcoin.example.net") == "btc"


def test_bitcoincash_host_is_never_read_as_bitcoin():
    assert coin.classify_by_pool_url("solo.bitcoincash.example.org") == "bch"
    assert coin.classify_by_pool_url("bitcoincash-solo.example:3333") == "bch"
    # Glued together with no separator, so only the substring scan can see it.
    assert coin.classify_by_pool_url("solobchpool.example") == "bch"


def test_hostname_without_a_coin_hint_is_unclassified():
    assert coin.classify_by_pool_url("solo.examplepool.org:3333") is None
    assert coin.classify_by_pool_url(None) is None
    assert coin.classify_by_pool_url("") is None


# ---------- active pool selection ----------

def test_active_pool_prefers_the_flagged_slot():
    sample = _sample(
        pools=[
            _pool(url="fallback.example:3333", user="a", active=False),
            _pool(url="primary.example:3333", user="b", active=True),
        ]
    )
    assert coin.active_pool(sample) == ("primary.example:3333", "b")


def test_active_pool_falls_back_to_first_slot_then_to_legacy_scalars():
    no_flag = _sample(pools=[_pool(url="only.example:3333", user="a")])
    assert coin.active_pool(no_flag) == ("only.example:3333", "a")

    legacy = _sample(pool_url="legacy.example:3333", worker="w")
    assert coin.active_pool(legacy) == ("legacy.example:3333", "w")

    assert coin.active_pool(None) == (None, None)


# ---------- the full cascade ----------

def test_override_beats_a_conflicting_stratum_reading():
    miner = {"id": 1, "coin_override": "bch"}
    sample = _sample(network_difficulty=1.2e14)   # unmistakably BTC
    assert coin.classify(miner, sample, REFS) == ("bch", coin.SOURCE_OVERRIDE)


def test_override_survives_an_offline_miner_with_no_sample():
    miner = {"id": 1, "coin_override": "btc"}
    assert coin.classify(miner, None, REFS) == ("btc", coin.SOURCE_OVERRIDE)


def test_corrupt_override_falls_through_to_detection():
    miner = {"id": 1, "coin_override": "dogecoin"}
    sample = _sample(network_difficulty=4.0e11)
    assert coin.classify(miner, sample, REFS) == ("bch", coin.SOURCE_STRATUM)


def test_cascade_order_stratum_then_address_then_hostname():
    miner = {"id": 1}

    stratum = _sample(
        network_difficulty=1.2e14,
        pool_url="bch.example.org:3333",       # contradicted by stratum
        worker="bitcoincash:qr9.w",
    )
    assert coin.classify(miner, stratum, REFS) == ("btc", coin.SOURCE_STRATUM)

    address = _sample(pool_url="solo.examplepool.org:3333", worker="bc1qabc.w")
    assert coin.classify(miner, address, REFS) == ("btc", coin.SOURCE_ADDRESS)

    hostname = _sample(pool_url="bch.examplepool.org:3333", worker="1LegacyAddr.w")
    assert coin.classify(miner, hostname, REFS) == ("bch", coin.SOURCE_POOL)


def test_nothing_to_go_on_is_unclassified_not_guessed():
    # The Canaan/LuxOS/Braiins case: no stratum difficulty, a neutral pool
    # hostname and a legacy address. Must stay unknown so the caller keeps
    # this hashrate out of both coins' odds.
    miner = {"id": 1}
    sample = _sample(
        pool_url="solo.examplepool.org:3333",
        worker="1BoatSLRHtKNngkdXEeobR76b53LETtpyT.rig",
    )
    assert coin.classify(miner, sample, REFS) == (None, None)


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

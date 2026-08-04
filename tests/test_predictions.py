# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the solo-mining odds builder (backend/predictions.py).

Covers the contract the Analytics widget relies on:

  * the Poisson maths is right — E[T] = D · 2^32 / H, and P(t) follows
    1 - exp(-t / E[T]);
  * a mixed BTC/BCH fleet produces one group per coin, each pairing that
    coin's OWN hashrate with that coin's OWN difficulty — the bug this
    module exists to fix was the whole fleet being measured against
    whichever difficulty happened to be read first;
  * a group's difficulty comes from its miners' stratum reading when they
    have one, and from the explorer reference otherwise;
  * unclassified miners get their own group with no odds, and their
    hashrate is never folded into a coin's estimate;
  * ``beat_best`` stays fleet-wide (share difficulty is chain-independent);
  * the pre-``groups`` payload keys keep working for an older frontend.

All fixtures below are fictional. Runs under pytest, or standalone:
``python tests/test_predictions.py``.
"""
from __future__ import annotations

import math
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import predictions  # noqa: E402
from backend.predictions import (  # noqa: E402
    build_prediction_payload,
    group_fleet_by_coin,
    prediction_window,
)

REFS = {"btc": 1.2e14, "bch": 4.0e11}


def _sample(**overrides):
    defaults = dict(
        online=True,
        hashrate_ths=1.0,
        network_difficulty=None,
        pool_url=None,
        worker=None,
        pools=[],
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _miner(mid, **overrides):
    row = {"id": mid, "name": f"miner{mid}", "host": f"10.0.0.{mid}"}
    row.update(overrides)
    return row


def _group(groups, coin):
    return next(g for g in groups if g["coin"] == coin)


# ---------- the Poisson window ----------

def test_expected_time_matches_the_closed_form():
    # 1 TH/s against difficulty 1000: E[T] = 1000 · 2^32 / 1e12 seconds.
    window = prediction_window(1.0, 1000.0)
    expected = 1000.0 * (2.0 ** 32) / 1e12
    assert math.isclose(window["expected_time_s"], expected, rel_tol=1e-12)


def test_probabilities_follow_the_exponential_cdf():
    window = prediction_window(1.0, 1000.0)
    rate = 1e12 / (1000.0 * (2.0 ** 32))
    for key, seconds in predictions.HORIZONS:
        expected = 1.0 - math.exp(-rate * seconds)
        assert math.isclose(window["probability"][key], expected, rel_tol=1e-12)


def test_probability_rises_with_the_horizon_and_stays_bounded():
    window = prediction_window(0.5, 1.2e14)
    p1h, p24h, p7d = (window["probability"][k] for k, _ in predictions.HORIZONS)
    assert 0.0 <= p1h <= p24h <= p7d <= 1.0


def test_window_is_none_when_either_input_is_unusable():
    assert prediction_window(0, 1000.0) is None
    assert prediction_window(None, 1000.0) is None
    assert prediction_window(-1.0, 1000.0) is None
    assert prediction_window(1.0, 0) is None
    assert prediction_window(1.0, None) is None
    assert prediction_window("fast", 1000.0) is None


def test_astronomical_wait_does_not_overflow():
    # A tiny miner against a huge difficulty: P must underflow to ~0, and
    # the expected time must stay a finite number rather than inf/NaN.
    window = prediction_window(1e-6, 1e18)
    assert math.isfinite(window["expected_time_s"])
    assert window["probability"]["1h"] >= 0.0


# ---------- grouping a mixed fleet ----------

def test_mixed_fleet_splits_hashrate_per_coin():
    miners = [_miner(1), _miner(2), _miner(3)]
    samples = {
        1: _sample(hashrate_ths=2.0, network_difficulty=1.2e14),   # BTC
        2: _sample(hashrate_ths=3.0, network_difficulty=1.2e14),   # BTC
        3: _sample(hashrate_ths=0.5, network_difficulty=4.0e11),   # BCH
    }
    groups, total = group_fleet_by_coin(miners, samples, REFS)

    assert total == 5.5
    assert _group(groups, "btc")["hashrate_ths"] == 5.0
    assert _group(groups, "bch")["hashrate_ths"] == 0.5
    assert _group(groups, "btc")["miner_ids"] == [1, 2]
    assert _group(groups, "bch")["miner_count"] == 1


def test_each_group_is_measured_against_its_own_difficulty():
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=1.0, network_difficulty=1.2e14),
        2: _sample(hashrate_ths=1.0, network_difficulty=4.0e11),
    }
    groups, _ = group_fleet_by_coin(miners, samples, REFS)

    btc, bch = _group(groups, "btc"), _group(groups, "bch")
    assert btc["network_difficulty"] == 1.2e14
    assert bch["network_difficulty"] == 4.0e11
    # Equal hashrate, 300× easier chain → 300× shorter expected wait. The
    # old fleet-wide code gave both the same number.
    ratio = btc["find_block"]["expected_time_s"] / bch["find_block"]["expected_time_s"]
    assert math.isclose(ratio, 1.2e14 / 4.0e11, rel_tol=1e-9)


def test_offline_miners_contribute_nothing():
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=2.0, network_difficulty=1.2e14),
        2: _sample(online=False, hashrate_ths=99.0, network_difficulty=1.2e14),
    }
    groups, total = group_fleet_by_coin(miners, samples, REFS)
    assert total == 2.0
    assert _group(groups, "btc")["hashrate_ths"] == 2.0
    assert _group(groups, "btc")["miner_count"] == 1


def test_group_difficulty_falls_back_to_the_explorer_reference():
    # A Canaan pinned to BCH by hand: no stratum reading anywhere in the
    # group, so the reference difficulty has to carry it.
    miners = [_miner(1, coin_override="bch")]
    samples = {1: _sample(hashrate_ths=6.0)}
    groups, _ = group_fleet_by_coin(miners, samples, REFS)

    bch = _group(groups, "bch")
    assert bch["network_difficulty"] == 4.0e11
    assert bch["difficulty_source"] == "explorer"
    assert bch["coin_sources"] == ["override"]
    assert bch["find_block"] is not None


def test_stratum_difficulty_wins_over_the_reference_and_takes_the_highest():
    # Across a retarget one miner can still be advertising the old epoch;
    # the freshest (highest) live value is the one to grade against.
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=1.0, network_difficulty=1.19e14),
        2: _sample(hashrate_ths=1.0, network_difficulty=1.23e14),
    }
    groups, _ = group_fleet_by_coin(miners, samples, REFS)

    btc = _group(groups, "btc")
    assert btc["network_difficulty"] == 1.23e14
    assert btc["difficulty_source"] == "stratum"


def test_unclassified_miners_are_isolated_and_get_no_odds():
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=1.0, network_difficulty=1.2e14),
        2: _sample(hashrate_ths=4.0, pool_url="solo.examplepool.org:3333"),
    }
    groups, total = group_fleet_by_coin(miners, samples, REFS)

    unknown = _group(groups, None)
    assert unknown["hashrate_ths"] == 4.0
    assert unknown["find_block"] is None
    assert unknown["network_difficulty"] is None
    # The unknown 4 TH/s must not inflate BTC's odds...
    assert _group(groups, "btc")["hashrate_ths"] == 1.0
    # ...but it still counts toward the fleet total the UI displays.
    assert total == 5.0


def test_groups_are_ordered_by_hashrate_with_unknown_last():
    miners = [_miner(1), _miner(2), _miner(3)]
    samples = {
        1: _sample(hashrate_ths=0.5, network_difficulty=1.2e14),   # btc
        2: _sample(hashrate_ths=9.0, network_difficulty=4.0e11),   # bch
        3: _sample(hashrate_ths=99.0, pool_url="solo.examplepool.org"),
    }
    groups, _ = group_fleet_by_coin(miners, samples, REFS)
    assert [g["coin"] for g in groups] == ["bch", "btc", None]


def test_labels_and_tickers_ride_along_for_the_ui():
    miners = [_miner(1)]
    samples = {1: _sample(network_difficulty=4.0e11)}
    groups, _ = group_fleet_by_coin(miners, samples, REFS)
    assert groups[0]["label"] == "Bitcoin Cash"
    assert groups[0]["ticker"] == "BCH"


# ---------- the full payload ----------

def test_payload_reports_groups_and_a_fleet_wide_beat_best():
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=2.0, network_difficulty=1.2e14),
        2: _sample(hashrate_ths=1.0, network_difficulty=4.0e11),
    }
    best = {"value": 1.0e9, "miner_name": "Lucky"}
    out = build_prediction_payload(miners, samples, REFS, best)

    assert out["coin"] == "auto"
    assert out["fleet_hashrate_ths"] == 3.0
    assert {g["coin"] for g in out["groups"]} == {"btc", "bch"}
    # beat_best uses the WHOLE fleet: any SHA-256 miner can set the record.
    expected = 1.0e9 * (2.0 ** 32) / (3.0 * 1e12)
    assert math.isclose(
        out["predictions"]["beat_best"]["expected_time_s"], expected, rel_tol=1e-12
    )


def test_legacy_keys_mirror_the_dominant_group():
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=0.2, network_difficulty=1.2e14),   # btc, small
        2: _sample(hashrate_ths=9.0, network_difficulty=4.0e11),   # bch, big
    }
    out = build_prediction_payload(miners, samples, REFS, None)

    bch = _group(out["groups"], "bch")
    assert out["network_difficulty"] == bch["network_difficulty"]
    assert out["predictions"]["find_block"] == bch["find_block"]


def test_forced_coin_is_a_whole_fleet_what_if():
    miners = [_miner(1), _miner(2)]
    samples = {
        1: _sample(hashrate_ths=2.0, network_difficulty=1.2e14),
        2: _sample(hashrate_ths=1.0, network_difficulty=4.0e11),
    }
    out = build_prediction_payload(
        miners, samples, REFS, None, forced_coin="bch", forced_difficulty=4.0e11
    )

    assert out["coin"] == "bch"
    # The headline uses all 3 TH/s, not just the 1 TH/s actually on BCH...
    expected = 4.0e11 * (2.0 ** 32) / (3.0 * 1e12)
    assert math.isclose(
        out["predictions"]["find_block"]["expected_time_s"], expected, rel_tol=1e-12
    )
    # ...while the groups keep reporting the real per-coin split.
    assert _group(out["groups"], "bch")["hashrate_ths"] == 1.0


def test_forced_coin_without_a_difficulty_omits_the_estimate():
    miners = [_miner(1)]
    samples = {1: _sample(hashrate_ths=2.0, network_difficulty=1.2e14)}
    out = build_prediction_payload(
        miners, samples, REFS, None, forced_coin="bch", forced_difficulty=None
    )
    assert out["predictions"]["find_block"] is None


def test_empty_or_offline_fleet_yields_no_estimates():
    out = build_prediction_payload([], {}, REFS, {"value": 1.0e9})
    assert out["fleet_hashrate_ths"] is None
    assert out["groups"] == []
    assert out["predictions"]["beat_best"] is None
    assert out["predictions"]["find_block"] is None
    assert out["network_difficulty"] is None


def test_fleet_of_only_unclassified_miners_reports_no_headline():
    miners = [_miner(1)]
    samples = {1: _sample(hashrate_ths=3.0, pool_url="solo.examplepool.org")}
    out = build_prediction_payload(miners, samples, REFS, None)

    assert out["fleet_hashrate_ths"] == 3.0
    assert out["predictions"]["find_block"] is None   # never invented
    assert out["groups"][0]["coin"] is None


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))

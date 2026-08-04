# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the /api/halo payload builder (backend/halo.py).

Covers the contract the consumer relies on:

  * ``ths`` is the TOTAL fleet hashrate in TH/s, summed over ONLINE
    miners only (no unit conversion, no offline ghosts);
  * ``miners`` / ``online`` reflect only currently-online miners;
  * ``net_diff`` takes the highest live network difficulty, falling back
    to the cached value and then a sane constant;
  * ``best_diff`` / ``best_alltime`` / ``top`` come from the best-record
    aggregates, ``top`` always padded to 3;
  * ``last_diff`` + ``miner`` follow the latest notable share, with a
    sensible fallback when none has been logged;
  * ``share_seq`` is strictly monotonic — it ticks up on new shares and
    never rewinds when a miner reboots and its accepted counter resets.

All fixtures below are fictional. Runs under pytest, or standalone:
``python tests/test_halo.py``.
"""
from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import halo  # noqa: E402
from backend.halo import DEFAULT_NET_DIFF, build_halo_payload, reset_share_seq  # noqa: E402


def _sample(**overrides):
    """A live MinerSample stand-in with the fields the builder reads."""
    defaults = dict(
        online=True,
        hashrate_ths=1.5,
        accepted=100,
        network_difficulty=1.0e14,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _miner(mid, name):
    return {"id": mid, "name": name, "model": "Demo Rig", "host": f"10.0.0.{mid}"}


def _best(session=None, alltime=None):
    return {"session": session, "alltime": alltime}


def _build(**kw):
    """build_halo_payload with sane defaults; reset share_seq each call."""
    reset_share_seq()
    params = dict(
        miners=[],
        samples={},
        best=_best(),
        top_records=[],
        latest_share=None,
        net_diff_fallback=None,
    )
    params.update(kw)
    return build_halo_payload(**params)


# ---------- aggregation: ths / miners / online ----------

def test_ths_sums_online_only_in_ths():
    miners = [_miner(1, "Lucky"), _miner(2, "Garage"), _miner(3, "Attic")]
    samples = {
        1: _sample(hashrate_ths=1.5),
        2: _sample(hashrate_ths=2.5),
        3: _sample(online=False, hashrate_ths=9.99),
    }
    out = _build(miners=miners, samples=samples)
    assert out["ths"] == 4.0             # 1.5 + 2.5, offline one excluded
    assert out["miners"] == 2
    assert out["online"] is True


def test_empty_fleet_is_offline():
    out = _build(miners=[_miner(1, "Lucky")], samples={})
    assert out["miners"] == 0
    assert out["online"] is False
    assert out["ths"] == 0.0
    assert out["net_diff"] == DEFAULT_NET_DIFF   # no live, no fallback


# ---------- net_diff ----------

def test_net_diff_prefers_highest_live_value():
    miners = [_miner(1, "a"), _miner(2, "b")]
    samples = {
        1: _sample(network_difficulty=1.1e14),
        2: _sample(network_difficulty=1.3e14),
    }
    out = _build(miners=miners, samples=samples, net_diff_fallback=9.9e9)
    assert out["net_diff"] == 1.3e14


def test_net_diff_falls_back_to_cache_when_no_live():
    miners = [_miner(1, "a")]
    samples = {1: _sample(network_difficulty=None)}
    out = _build(miners=miners, samples=samples, net_diff_fallback=1.2e14)
    assert out["net_diff"] == 1.2e14


# ---------- net_diff on a mixed BTC/BCH fleet ----------
# The gauge draws last_diff against net_diff, so both must describe the
# same chain. "Highest difficulty in the fleet" always resolves to BTC and
# made every BCH share look ~1000x further from a block than it really was.

REFS = {"btc": 1.2e14, "bch": 4.0e11}


def test_net_diff_follows_the_coin_of_the_displayed_share():
    miners = [_miner(1, "BtcRig"), _miner(2, "BchRig")]
    samples = {
        1: _sample(network_difficulty=1.2e14),
        2: _sample(network_difficulty=4.0e11),
    }
    # The newest share came from the BCH miner, so grade it against BCH.
    live = {2: {"submitted_total": 10, "last_diff": 5.0e8, "last_ts": 200.0, "name": "BchRig"}}
    out = _build(miners=miners, samples=samples, live_shares=live, references=REFS)
    assert out["net_diff"] == 4.0e11

    # Same fleet, newest share from the BTC miner → the BTC difficulty.
    live = {1: {"submitted_total": 10, "last_diff": 9.0e9, "last_ts": 300.0, "name": "BtcRig"}}
    out = _build(miners=miners, samples=samples, live_shares=live, references=REFS)
    assert out["net_diff"] == 1.2e14


def test_net_diff_uses_the_reference_when_that_miner_reports_none():
    # A Canaan pinned to BCH by hand: no stratum reading of its own, so the
    # reference difficulty for its coin has to carry the gauge.
    miners = [_miner(1, "BtcRig"), {**_miner(2, "Avalon"), "coin_override": "bch"}]
    samples = {
        1: _sample(network_difficulty=1.2e14),
        2: _sample(network_difficulty=None),
    }
    live = {2: {"submitted_total": 4, "last_diff": 1.0e8, "last_ts": 500.0, "name": "Avalon"}}
    out = _build(miners=miners, samples=samples, live_shares=live, references=REFS)
    assert out["net_diff"] == 4.0e11


def test_net_diff_attributes_a_persisted_notable_share_to_its_miner():
    miners = [_miner(1, "BtcRig"), _miner(2, "BchRig")]
    samples = {
        1: _sample(network_difficulty=1.2e14),
        2: _sample(network_difficulty=4.0e11),
    }
    latest = {"miner_id": 2, "ts": 900, "share_difficulty": 7.0e8, "name": "BchRig"}
    out = _build(miners=miners, samples=samples, latest_share=latest, references=REFS)
    assert out["net_diff"] == 4.0e11
    assert out["last_diff"] == 7.0e8


def test_net_diff_keeps_the_fleet_wide_behaviour_without_references():
    # No references passed (cold cache): fall back to the old rule rather
    # than dropping the gauge.
    miners = [_miner(1, "a"), _miner(2, "b")]
    samples = {
        1: _sample(network_difficulty=1.1e14),
        2: _sample(network_difficulty=1.3e14),
    }
    out = _build(miners=miners, samples=samples)
    assert out["net_diff"] == 1.3e14


# ---------- best / top / last share ----------

def test_best_and_top_and_last_share():
    miners = [_miner(1, "Lucky")]
    samples = {1: _sample()}
    best = _best(
        session={"value": 9_000_000.0, "miner_name": "Garage"},
        alltime={"value": 5_000_000_000.0, "miner_name": "Lucky"},
    )
    top = [{"value": 5.0e9}, {"value": 2.0e9}]   # only two — must pad to 3
    latest = {"share_difficulty": 1_500_000.0, "name": "Attic"}
    out = _build(miners=miners, samples=samples, best=best, top_records=top, latest_share=latest)

    assert out["best_diff"] == 9_000_000.0
    assert out["best_alltime"] == 5_000_000_000.0
    assert out["top"] == [5.0e9, 2.0e9, 0.0]
    assert out["last_diff"] == 1_500_000.0
    assert out["miner"] == "Attic"


def test_last_share_fallback_to_session_best():
    miners = [_miner(1, "Lucky")]
    samples = {1: _sample()}
    best = _best(session={"value": 750_000.0, "miner_name": "Lucky"})
    out = _build(miners=miners, samples=samples, best=best, latest_share=None)
    assert out["last_diff"] == 750_000.0
    assert out["miner"] == "Lucky"


# ---------- share_seq: monotonic counter ----------

def test_share_seq_ticks_up_on_new_shares():
    miners = [_miner(1, "Lucky")]
    reset_share_seq()
    s1 = build_halo_payload(miners, {1: _sample(accepted=100)}, _best(), [], None, None)
    s2 = build_halo_payload(miners, {1: _sample(accepted=105)}, _best(), [], None, None)
    assert s2["share_seq"] > s1["share_seq"]
    assert s2["share_seq"] - s1["share_seq"] == 5


def test_share_seq_never_rewinds_on_miner_reboot():
    miners = [_miner(1, "Lucky")]
    reset_share_seq()
    a = build_halo_payload(miners, {1: _sample(accepted=1000)}, _best(), [], None, None)
    # miner reboots: its cumulative accepted counter resets to a low number
    b = build_halo_payload(miners, {1: _sample(accepted=3)}, _best(), [], None, None)
    # new shares come in after the reboot
    c = build_halo_payload(miners, {1: _sample(accepted=8)}, _best(), [], None, None)
    assert b["share_seq"] >= a["share_seq"]          # never travels backwards
    assert c["share_seq"] == b["share_seq"] + 5      # resumes ticking immediately


# ---------- live per-share feed (AxeOS) ----------

def _live(submitted_total, last_diff, last_ts, name="Lucky"):
    return {
        "submitted_total": submitted_total,
        "last_diff": last_diff,
        "last_ts": last_ts,
        "name": name,
    }


def test_live_last_share_wins_over_notable_when_newer():
    miners = [_miner(1, "Lucky")]
    samples = {1: _sample()}
    notable = {"share_difficulty": 9_000_000.0, "name": "OldNotable", "ts": 1000}
    live = {1: _live(50, 1_234_567.0, 2000, name="Lucky")}
    out = _build(miners=miners, samples=samples, latest_share=notable, live_shares=live)
    assert out["last_diff"] == 1_234_567.0
    assert out["miner"] == "Lucky"


def test_notable_wins_when_live_is_older():
    miners = [_miner(1, "Lucky")]
    samples = {1: _sample()}
    notable = {"share_difficulty": 9_000_000.0, "name": "BigOne", "ts": 5000}
    live = {1: _live(50, 1_000.0, 1000, name="Lucky")}
    out = _build(miners=miners, samples=samples, latest_share=notable, live_shares=live)
    assert out["last_diff"] == 9_000_000.0
    assert out["miner"] == "BigOne"


def test_share_seq_uses_submitted_total_for_axeos():
    miners = [_miner(1, "Lucky")]
    reset_share_seq()
    s1 = build_halo_payload(miners, {1: _sample(accepted=999)}, _best(), [], None, None,
                            live_shares={1: _live(200, 5.0, 10)})
    s2 = build_halo_payload(miners, {1: _sample(accepted=999)}, _best(), [], None, None,
                            live_shares={1: _live(207, 5.0, 11)})
    assert s2["share_seq"] - s1["share_seq"] == 7    # submitted_total drove it, not accepted


def test_share_seq_mixed_fleet_live_plus_accepted():
    miners = [_miner(1, "Axe"), _miner(2, "Avalon")]
    reset_share_seq()
    samples = {1: _sample(accepted=999), 2: _sample(accepted=100)}
    live = {1: _live(50, 5.0, 10)}                   # only miner 1 streams per-share
    out = build_halo_payload(miners, samples, _best(), [], None, None, live_shares=live)
    assert out["share_seq"] == 150                   # 50 submitted + 100 accepted


# ---------- Bitcoin price block ----------

def test_btc_fields_included_and_rounded_when_known():
    out = _build(miners=[], samples={}, btc_price=70770.42, btc_change=-3.94)
    assert out["btc_price"] == 70770                 # rounded to integer
    assert out["btc_change"] == -3.94


def test_btc_fields_omitted_when_unknown():
    out = _build(miners=[], samples={})
    assert "btc_price" not in out
    assert "btc_change" not in out


def test_btc_change_omitted_when_only_price_known():
    out = _build(miners=[], samples={}, btc_price=65000, btc_change=None)
    assert out["btc_price"] == 65000
    assert "btc_change" not in out


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'all passed' if not failures else str(failures) + ' failed'}")
    sys.exit(1 if failures else 0)

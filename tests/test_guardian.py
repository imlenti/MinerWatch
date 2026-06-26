# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the Guardian runtime frequency governor.

Covers the pure decision function ``decide_frequency`` (the control policy)
and the windowed HW-error-% helper ``_hw_error_pct`` (counter-delta logic).
Both are pure / state-only, so they can be exercised without a miner, a
poller, or the event loop.

Runs under pytest, or standalone: ``python tests/test_guardian.py``.
"""
from __future__ import annotations

import pathlib
import sys
import types
import pytest

# Make the repo root importable whether invoked via pytest or directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend.guardian import (  # noqa: E402
    _GuardianState,
    _reject_pct,
    decide_frequency,
    decide_point,
)

# Defaults mirroring GuardianCfg / the friend's field-tested values.
DEFAULTS = dict(
    ceiling_mhz=600,
    floor_mhz=400,
    temp_high_c=70.0,
    temp_low_c=65.0,
    hw_error_pct_max=1.1,
    step_down_temp_mhz=20,
    step_down_err_mhz=10,
    step_up_mhz=10,
)


def decide(**over):
    """decide_frequency with the standard thresholds, returning the target."""
    kwargs = {**DEFAULTS, **over}
    return decide_frequency(**kwargs)


# ---- the control law --------------------------------------------------------

def test_vr_hot_steps_down_20():
    target, reason = decide(current_freq=550, temp_c=72.0, hw_error_pct=0.0)
    assert target == 530, reason


def test_errors_high_step_down_10():
    target, reason = decide(current_freq=550, temp_c=67.0, hw_error_pct=1.5)
    assert target == 540, reason


def test_vr_cool_steps_up_10():
    target, reason = decide(current_freq=550, temp_c=60.0, hw_error_pct=0.0)
    assert target == 560, reason


def test_deadband_holds():
    # Between vr_low (65) and vr_high (70), errors fine → no change.
    target, reason = decide(current_freq=550, temp_c=67.0, hw_error_pct=0.2)
    assert target == 550
    assert "hold" in reason


def test_up_step_capped_at_ceiling():
    # Cool VR wants +10 but we're at the ceiling already → hold.
    target, reason = decide(current_freq=600, temp_c=60.0, hw_error_pct=0.0)
    assert target == 600
    assert "limit" in reason


def test_down_step_clamped_at_floor():
    target, reason = decide(current_freq=400, temp_c=80.0, hw_error_pct=0.0)
    assert target == 400
    assert "limit" in reason


def test_above_ceiling_is_capped_first():
    # User manually overclocked past the cap: pull back to the ceiling even
    # though the VR is cool (which would otherwise want to push UP).
    target, reason = decide(current_freq=700, temp_c=55.0, hw_error_pct=0.0)
    assert target == 600
    assert "cap" in reason


def test_below_floor_is_raised_first():
    target, reason = decide(current_freq=350, temp_c=80.0, hw_error_pct=5.0)
    assert target == 400
    assert "floor" in reason


# ---- priority between branches ---------------------------------------------

def test_vr_hot_beats_errors():
    # Both VR>70 and errors high → the bigger (VR) cut wins.
    target, _ = decide(current_freq=550, temp_c=75.0, hw_error_pct=9.9)
    assert target == 530  # -20, not -10


def test_errors_beat_cool_recovery():
    # VR is cool (would want +10) but errors are high → safety wins, step down.
    target, reason = decide(current_freq=550, temp_c=60.0, hw_error_pct=2.0)
    assert target == 540, reason


# ---- missing sensors --------------------------------------------------------

def test_no_vr_no_errors_holds():
    target, reason = decide(current_freq=550, temp_c=None, hw_error_pct=None)
    assert target == 550
    assert "hold" in reason


def test_no_errors_vr_governs():
    # Too few shares this interval → reject term inactive (None), VR governs.
    target, _ = decide(current_freq=550, temp_c=72.0, hw_error_pct=None)
    assert target == 530


def test_no_vr_errors_still_act():
    target, _ = decide(current_freq=550, temp_c=None, hw_error_pct=3.0)
    assert target == 540


# ---- defensive: floor above ceiling shouldn't brick the loop ---------------

def test_floor_above_ceiling_clamped():
    # floor(620) > ceiling(600): the function clamps floor to ceiling, so a
    # cool-VR up-step lands on the ceiling and holds rather than exploding.
    target, _ = decide(current_freq=600, floor_mhz=620, temp_c=60.0, hw_error_pct=0.0)
    assert target == 600


# ---- reject-rate windowed helper -------------------------------------------

MIN_SHARES = 20


def _sample(accepted, rejected):
    return types.SimpleNamespace(accepted=accepted, rejected=rejected)


def test_reject_first_tick_is_none_and_sets_baseline():
    st = _GuardianState()
    pct = _reject_pct(st, _sample(1000, 5), MIN_SHARES)
    assert pct is None
    assert st.prev_accepted == 1000 and st.prev_rejected == 5


def test_reject_computes_delta_over_interval():
    st = _GuardianState()
    _reject_pct(st, _sample(1000, 5), MIN_SHARES)          # baseline
    # +95 accepted / +5 rejected → 5 / 100 = 5.0%
    pct = _reject_pct(st, _sample(1095, 10), MIN_SHARES)
    assert pct is not None
    assert abs(pct - 5.0) < 1e-9


def test_reject_min_shares_guard_returns_none():
    st = _GuardianState()
    _reject_pct(st, _sample(1000, 5), MIN_SHARES)          # baseline
    # Only 11 shares this interval (< MIN_SHARES) → too few to trust → None,
    # so a single stale share can't spike the rate and force a throttle.
    pct = _reject_pct(st, _sample(1010, 6), MIN_SHARES)
    assert pct is None


def test_reject_counter_reset_returns_none():
    st = _GuardianState()
    _reject_pct(st, _sample(5000, 50), MIN_SHARES)
    # Miner rebooted → counters dropped: must not produce a garbage %.
    pct = _reject_pct(st, _sample(10, 1), MIN_SHARES)
    assert pct is None
    # Baseline re-anchored to the new (lower) counters.
    assert st.prev_accepted == 10 and st.prev_rejected == 1


def test_reject_zero_rejects_is_zero_pct():
    st = _GuardianState()
    _reject_pct(st, _sample(1000, 0), MIN_SHARES)
    pct = _reject_pct(st, _sample(1100, 0), MIN_SHARES)    # 100 shares, 0 rej
    assert pct == 0.0


# ---- source-agnostic temperature signal (chip vs VR) -----------------------

def test_source_label_flavours_the_reason():
    # The same control law drives either sensor; only the label changes. Feed a
    # hot reading with the chip label and the reason should name the chip.
    target, reason = decide(
        current_freq=550, temp_c=80.0, hw_error_pct=0.0, source_label="Chip"
    )
    assert target == 530
    assert reason.startswith("Chip")


def test_temp_band_picks_source_defaults():
    from backend.config import GuardianCfg

    cfg = GuardianCfg()
    assert cfg.temp_band("vr") == (cfg.vr_high_c, cfg.vr_low_c)
    assert cfg.temp_band("chip") == (cfg.chip_high_c, cfg.chip_low_c)
    # Unset / unknown falls back to the VR band (legacy behaviour).
    assert cfg.temp_band(None) == (cfg.vr_high_c, cfg.vr_low_c)
    assert cfg.temp_band("bogus") == (cfg.vr_high_c, cfg.vr_low_c)


def test_temp_band_bitforge_vr_runs_hotter():
    """The BitForge's TPS546 sits ~71 °C at stock (vs the Bitaxe band's
    70 °C ceiling), so the family gets its own VR band; chip mode and the
    other families keep the shared defaults."""
    from backend.config import GuardianCfg

    cfg = GuardianCfg()
    assert cfg.temp_band("vr", "bitforge") == (
        cfg.bitforge_vr_high_c,
        cfg.bitforge_vr_low_c,
    )
    assert cfg.bitforge_vr_high_c > cfg.vr_high_c
    # Same hysteresis deadband as the stock band.
    assert (cfg.bitforge_vr_high_c - cfg.bitforge_vr_low_c) == (
        cfg.vr_high_c - cfg.vr_low_c
    )
    # The band must stay under the co-tuner's VR hard cutoff.
    assert cfg.bitforge_vr_high_c < cfg.vr_cutoff_c
    assert cfg.temp_band("chip", "bitforge") == (cfg.chip_high_c, cfg.chip_low_c)
    assert cfg.temp_band("vr", "bitaxe") == (cfg.vr_high_c, cfg.vr_low_c)
    assert cfg.temp_band("vr", None) == (cfg.vr_high_c, cfg.vr_low_c)


def test_vin_band_scales_for_12v_boards():
    """The configured Vin window is 5 V-shaped (4800-5500). A 12 V board
    (NerdQAxe, BitForge Nano) reports ~12000 mV and must get the window
    rescaled by 12/5 instead of hard-tripping; 5 V boards and unknown
    readings pass through untouched."""
    from backend.guardian import vin_band

    assert vin_band(5050.0, 4800.0, 5500.0) == (4800.0, 5500.0)
    assert vin_band(None, 4800.0, 5500.0) == (4800.0, 5500.0)
    lo, hi = vin_band(12015.0, 4800.0, 5500.0)
    assert (lo, hi) == (11520.0, 13200.0)
    assert lo < 12015.0 < hi
    # A genuinely sagging 12 V PSU still trips the scaled window.
    assert not (lo < 11300.0)


# ---- hashrate validity brake (theoretical) ---------------------------------

def test_invalid_steps_down_even_when_cool():
    # Source cool (would want +10) but hashrate below theoretical → back off.
    target, reason = decide(
        current_freq=550, temp_c=60.0, hw_error_pct=0.0, hashrate_invalid=True
    )
    assert target == 530  # -step_down_temp_mhz (the hr step defaults to it)
    assert "hashrate" in reason


def test_invalid_uses_its_own_step():
    target, _ = decide(
        current_freq=550, temp_c=60.0, hw_error_pct=0.0,
        hashrate_invalid=True, step_down_hr_mhz=30,
    )
    assert target == 520


def test_temp_hot_beats_invalid():
    # Both a temperature cut and an invalid-hashrate cut apply → temperature wins.
    target, reason = decide(
        current_freq=550, temp_c=80.0, hw_error_pct=0.0,
        hashrate_invalid=True, step_down_hr_mhz=40,
    )
    assert target == 530  # -20 temp, not -40 hr
    assert "°C" in reason


def test_invalid_beats_reject_and_recovery():
    # Invalid hashrate outranks the reject term and the cool-source recovery.
    target, reason = decide(
        current_freq=550, temp_c=60.0, hw_error_pct=9.9, hashrate_invalid=True
    )
    assert target == 530
    assert "hashrate" in reason


def test_valid_keeps_recovering():
    # Sanity: valid hashrate + cool source → recovery still steps up.
    target, _ = decide(
        current_freq=550, temp_c=60.0, hw_error_pct=0.0, hashrate_invalid=False
    )
    assert target == 560


def test_recovery_gated_off_holds():
    # Cool source would normally recover, but allow_recovery=False (hashrate not
    # yet verifiable, or not valid) → hold instead of climbing into instability.
    target, reason = decide(
        current_freq=550, temp_c=60.0, hw_error_pct=0.0,
        hashrate_invalid=False, allow_recovery=False,
    )
    assert target == 550
    assert "hold" in reason


# ---- Phase 2: V/F co-tuner (decide_point) ----------------------------------

DEFAULTS_PT = dict(
    current_volt=1150,
    ceiling_mhz=700,
    floor_mhz=400,
    volt_ceiling_mv=1250,
    volt_floor_mv=1000,
    temp_c=60.0,
    temp_high_c=70.0,
    temp_low_c=65.0,
    chip_c=55.0,
    chip_cutoff_c=70.0,
    vr_c=55.0,
    vr_cutoff_c=85.0,
    power_w=20.0,
    power_cutoff_w=40.0,
    vin_mv=5100.0,
    vin_min_mv=4800.0,
    vin_max_mv=5500.0,
    step_down_mhz=20,
    step_up_mhz=10,
    step_volt_mv=10,
)


def decide_pt(**over):
    return decide_point(**{**DEFAULTS_PT, **over})


def test_pt_safety_cutoff_backs_off_both():
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=False, valid=True, chip_c=72.0)
    assert (f, v) == (580, 1140), r
    assert "safety" in r


def test_pt_power_cutoff_backs_off_both():
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=False, valid=True, power_w=41.0)
    assert (f, v) == (580, 1140), r
    assert "power" in r


def test_pt_temp_over_limit_co_moves_down():
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=False, valid=True, temp_c=72.0)
    assert (f, v) == (580, 1140), r
    assert "temp" in r


def test_pt_invalid_raises_voltage_to_cure():
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=True, valid=False)
    assert (f, v) == (600, 1160), r  # +10 mV, frequency unchanged
    assert "cure" in r


def test_pt_invalid_drops_freq_when_voltage_maxed():
    f, v, r = decide_pt(current_freq=600, current_volt=1250, hashrate_invalid=True, valid=False)
    assert (f, v) == (580, 1250), r
    assert "MHz" in r


def test_pt_invalid_drops_freq_when_no_temp_headroom():
    # Near the temp limit → can't safely add volts → drop frequency instead.
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=True, valid=False, temp_c=69.0)
    assert (f, v) == (580, 1150), r


def test_pt_valid_and_cool_pushes_freq():
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=False, valid=True, temp_c=60.0)
    assert (f, v) == (610, 1150), r
    assert "MHz" in r


def test_pt_valid_deadband_holds():
    # Between low (65) and high (70): nothing to do.
    f, v, r = decide_pt(current_freq=600, hashrate_invalid=False, valid=True, temp_c=67.0)
    assert (f, v) == (600, 1150)
    assert "hold" in r


def test_pt_above_ceiling_caps():
    f, v, r = decide_pt(current_freq=720, hashrate_invalid=False, valid=True)
    assert f == 700
    assert "cap" in r


def test_govern_one_uses_recent_averages():
    import asyncio
    from unittest.mock import AsyncMock, Mock, patch
    from backend.miners.base import MinerSample
    from backend.guardian import guardian, _GuardianState

    miner = {"id": 1, "name": "miner1", "family": "bitaxe", "guardian_temp_source": "vr"}
    sample = MinerSample(
        family="bitaxe",
        host="10.0.0.1",
        online=True,
        frequency_mhz=550,
        temp_chip_c=60.0,
        temp_vr_c=68.0,
        hashrate_ths=2.5,
        power_w=20.0,
        accepted=100,
        rejected=0,
    )

    gcfg = Mock()
    gcfg.hashrate_average_window_seconds = 30
    gcfg.reject_min_shares = 10
    gcfg.reject_pct_max = 5.0
    gcfg.frequency_floor_mhz = 400
    gcfg.step_down_vr_mhz = 20
    gcfg.step_down_err_mhz = 10
    gcfg.step_up_mhz = 10
    gcfg.temp_band = Mock(return_value=(70.0, 67.0))
    gcfg.hashrate_settle_seconds = 0
    gcfg.valid_pct = 0.97
    gcfg.error_pct_max = 5.0
    gcfg.v2_voltage_enabled = False
    gcfg.cooldown_seconds = 0

    cfg = Mock()

    sample.expected_hashrate_ths = 2.0

    avg_metrics = {
        "hashrate_ths": 1.5,
        "power_w": 20.0,
        "temp_chip_c": 60.0,
        "temp_vr_c": 68.0,
    }

    async def run():
        mock_drv = AsyncMock()
        mock_drv.set_frequency.return_value = True
        with patch("backend.db.get_recent_metrics_average", AsyncMock(return_value=avg_metrics)) as mock_avg, \
             patch("backend.guardian.driver_for_record", Mock(return_value=mock_drv)) as mock_dfr, \
             patch.object(guardian, "_publish") as mock_publish, \
             patch("backend.guardian.decide_frequency") as mock_decide:

            mock_decide.return_value = (530, "hashrate below theoretical")

            state = _GuardianState()
            state.last_commanded_freq = 550
            guardian._states[1] = state

            await guardian._govern_one(miner, sample, gcfg, cfg)

            mock_avg.assert_called_once_with(1, 30)
            mock_decide.assert_called_once()
            kwargs = mock_decide.call_args.kwargs
            assert kwargs["hashrate_invalid"] is True

    asyncio.run(run())


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)

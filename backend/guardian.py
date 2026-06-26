# SPDX-License-Identifier: AGPL-3.0-only
"""Guardian — a runtime frequency governor for AxeOS miners (Bitaxe / Nerd*).

This is a continuous, *slow* control loop. It is a twin of the server-side
auto-fan PID in ``auto_control.py``, but acts on a different lever and a
different sensor:

  - the auto-fan PID is the FAST inner loop (5 s): it modulates the FAN to
    hold the CHIP temperature near a target;
  - the Guardian is the SLOW outer loop (default 5 min): it nudges the ASIC
    FREQUENCY to keep the VR (voltage-regulator) temperature and the
    rejected-share rate inside safe bounds, recovering frequency when cool.

Nothing else in MinerWatch governs the VR in a closed loop — the fan PID
and the 75 °C overheat watchdog both watch the *chip*. The VR is frequently
the real bottleneck, so a VR-driven frequency governor fills a genuine gap
rather than duplicating the fan logic. The two loops reinforce each other:
when the VR gets hot the Guardian cuts frequency → less power → the VR
*and* the chip cool → the fan PID eases off.

Control law (v1, frequency-only), evaluated once per ``interval_seconds``:

    VR temp   > vr_high_c        → frequency − step_down_vr_mhz   (safety)
    reject %  > reject_pct_max   → frequency − step_down_err_mhz  (safety)
    VR temp   < vr_low_c         → frequency + step_up_mhz        (recover)
    otherwise (deadband)         → hold

Down-actions (safety) take priority over the upward recovery, and every
result is clamped to the per-miner ``[floor, ceiling]``. The ceiling is the
user's "max frequency" — by default the miner's current frequency at the
moment the Guardian is enabled, but editable for expert users.

Why the cadence is the safety knob: AxeOS applies a frequency change LIVE
(no reboot — confirmed for both Bitaxe and Nerd*), so there is no downtime
cost per nudge. The limiting factor is instead the VR's *thermal inertia*:
after a change the VR keeps drifting for a minute or two. Ticking faster
than that would mean acting on a reading that hasn't finished responding,
which causes hunting. So the loop runs on a long interval (≥ the VR settle
time), and an optional ``cooldown_seconds`` can enforce extra settle time.

NVS wear: a frequency PATCH persists to the ESP32's flash. The governor
only writes when the target *differs* from the live frequency, so inside the
65–70 °C deadband it parks on an equilibrium frequency and stops writing.

Reversibility: this is an additive bolt-on. It lives in this module, reads
``poller.last_results``, uses only driver methods that already exist
(``set_frequency`` / ``poll``) and three per-miner columns on the ``miners``
table. It never changes voltage in v1 (see the v2 notes below and in
docs/guardian-design.md).

v2 (not active here): AxeOS also applies *voltage* changes live, which opens
a second lever — respond to a sustained reject rate by RAISING coreVoltage
(the proper fix for undervolt instability) instead of only cutting freq, and
optionally lower voltage alongside frequency cuts to preserve J/TH. Auto-
raising voltage 24/7 unattended is riskier (more heat/watts, closer to the
hardware limits), so it stays out of v1. The decision function and the
config carry the seams for it; see ``GuardianCfg.v2_*`` and the design doc.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import db
from .config import get_config
from .miners import driver_for_record
from .miners.base import MinerSample

log = logging.getLogger("minerwatch.guardian")

# Families this governor knows how to drive. All speak the AxeOS REST API
# and expose ``vrTemp``; the VR temperature is the primary control signal.
# bitforge (forge-os) lacks ``expectedHashrate``, so its validity test
# relies on the smallCoreCount × asicCount fallback.
GUARDIAN_FAMILIES = ("bitaxe", "nerdoctaxe", "bitforge")


# ============================================================================
# Pure decision function (no I/O — unit-tested in tests/test_guardian.py)
# ============================================================================

def decide_frequency(
    *,
    current_freq: int,
    ceiling_mhz: int,
    floor_mhz: int,
    temp_c: float | None,
    hw_error_pct: float | None,
    temp_high_c: float,
    temp_low_c: float,
    hw_error_pct_max: float,
    step_down_temp_mhz: int,
    step_down_err_mhz: int,
    step_up_mhz: int,
    source_label: str = "VR",
    hashrate_invalid: bool = False,
    step_down_hr_mhz: int | None = None,
    allow_recovery: bool = True,
    instability_label: str = "hashrate below theoretical",
) -> tuple[int, str]:
    """Decide the next frequency for one miner.

    Returns ``(target_freq_mhz, reason)``. ``target_freq_mhz == current_freq``
    means "hold" (the caller then writes nothing, sparing NVS). The function
    is deliberately pure so the policy can be reasoned about and tested in
    isolation from the driver/poller plumbing.

    The temperature signal is source-agnostic: ``temp_c`` is whichever sensor
    the caller chose (the VR by default, or the ASIC chip per-miner) and
    ``temp_high_c`` / ``temp_low_c`` are the matching thresholds; ``source_label``
    only flavours the human-readable reason string. ``hw_error_pct`` is the
    instability signal — in v1 the rejected-share % over the interval. Both
    ``temp_c`` and ``hw_error_pct`` may be ``None``: a ``None`` simply disables
    the branch that depends on it. So with no temperature reading the governor
    won't move on heat, and when too few shares landed in the interval to trust
    a reject % the error branch is skipped and temperature governs alone.

    ``hashrate_invalid`` is a second instability signal computed by the caller:
    True when the measured hashrate is below ``valid_pct`` of the *theoretical*
    hashrate for the current frequency (``freq * cores / 1e6`` TH/s) — ASIC
    hardware errors eating real work. It backs off by ``step_down_hr_mhz``
    (defaulting to ``step_down_temp_mhz``) and ranks just under the temperature
    safety cut. ``allow_recovery`` gates the upward step: frequency is pushed UP
    only when the current point is valid (and the caller could verify it), so a
    chip that can't keep up — or one whose hashrate can't yet be judged — is
    never pushed higher. This is what makes the governor stop chasing frequency
    into instability.
    """
    # Defensive: a mis-set floor above the ceiling must not brick the loop.
    if floor_mhz > ceiling_mhz:
        floor_mhz = ceiling_mhz

    # 0. Enforce the per-miner ceiling/floor first, regardless of sensors.
    #    The ceiling is the user's "max frequency": never run above it (e.g.
    #    if the user manually overclocked past the cap, pull it back down).
    if current_freq > ceiling_mhz:
        return ceiling_mhz, f"above max {ceiling_mhz} MHz → cap to ceiling"
    if current_freq < floor_mhz:
        return floor_mhz, f"below floor {floor_mhz} MHz → raise to floor"

    # 1..3 — the control law. Order encodes the priority: back off on heat,
    # then on instability, and only otherwise try to recover frequency.
    if temp_c is not None and temp_c > temp_high_c:
        target = current_freq - step_down_temp_mhz
        reason = (
            f"{source_label} {temp_c:.1f}°C > {temp_high_c:.0f}°C "
            f"→ -{step_down_temp_mhz} MHz"
        )
    elif hashrate_invalid:
        sd_hr = step_down_hr_mhz if step_down_hr_mhz is not None else step_down_temp_mhz
        target = current_freq - sd_hr
        reason = f"{instability_label} (instability) → -{sd_hr} MHz"
    elif hw_error_pct is not None and hw_error_pct > hw_error_pct_max:
        target = current_freq - step_down_err_mhz
        reason = (
            f"Reject {hw_error_pct:.2f}% > {hw_error_pct_max:.2f}% "
            f"→ -{step_down_err_mhz} MHz"
        )
    elif allow_recovery and temp_c is not None and temp_c < temp_low_c:
        target = current_freq + step_up_mhz
        reason = (
            f"{source_label} {temp_c:.1f}°C < {temp_low_c:.0f}°C "
            f"→ +{step_up_mhz} MHz"
        )
    else:
        return current_freq, "hold (within deadband)"

    target = max(floor_mhz, min(ceiling_mhz, target))
    if target == current_freq:
        # The desired move was clamped away by a limit (already at floor on a
        # down-step, or at ceiling on an up-step).
        return current_freq, "hold (at limit)"
    return target, reason


def vin_band(
    vin_mv: float | None, vin_min_mv: float, vin_max_mv: float
) -> tuple[float, float]:
    """Scale the configured Vin window to the board's input rail.

    The configured ``vin_min/max_mv`` describe a 5 V-input board (the
    original Bitaxe assumption: 4800–5500). 12 V-input boards — NerdQAxe,
    BitForge Nano — report ``voltage`` ≈ 12000 mV, which sits permanently
    above the 5 V window and would hard-trip the co-tuner on every tick.
    A reading above 8000 mV can only be a 12 V rail (no supported board
    runs between 5.5 V and 8 V), so the window scales by 12/5 — 11520–13200
    with the defaults — preserving the same relative sag/overshoot margins
    (a 12 V PSU sagging below ~11.5 V trips, exactly like 5 V below 4.8).
    """
    if vin_mv is not None and vin_mv > 8000:
        return vin_min_mv * 12 / 5, vin_max_mv * 12 / 5
    return vin_min_mv, vin_max_mv


def decide_point(
    *,
    current_freq: int,
    current_volt: int,
    ceiling_mhz: int,
    floor_mhz: int,
    volt_ceiling_mv: int,
    volt_floor_mv: int,
    temp_c: float | None,
    temp_high_c: float,
    temp_low_c: float,
    hashrate_invalid: bool,
    valid: bool,
    instability_label: str = "hashrate below theoretical",
    chip_c: float | None,
    chip_cutoff_c: float,
    vr_c: float | None,
    vr_cutoff_c: float,
    power_w: float | None,
    power_cutoff_w: float | None,
    vin_mv: float | None,
    vin_min_mv: float,
    vin_max_mv: float,
    step_down_mhz: int,
    step_up_mhz: int,
    step_volt_mv: int,
) -> tuple[int, int, str]:
    """Decide ``(target_freq_mhz, target_volt_mv, reason)`` for the V/F co-tuner.

    Pure function (no I/O), unit-tested. Used only when a miner has opted in to
    voltage co-tuning; otherwise the governor uses :func:`decide_frequency`
    (frequency-only). The clause order encodes the priority:

      0. clamp back inside the envelope (freq/volt ceiling/floor);
      1. hard safety cutoff (chip / VR / power / input-voltage) → drop BOTH;
      2. temperature over the user's limit → co-move DOWN (freq and volt) along
         the efficient curve;
      3. hashrate below theoretical (instability) → raise VOLTAGE to cure it,
         or, if voltage is maxed (or no thermal/power headroom), drop frequency;
      4. valid and cool → push FREQUENCY up for more hashrate;
      5. otherwise hold (park — no write).

    Returns the current point with a "hold" reason when nothing should change.
    """
    f, v = int(current_freq), int(current_volt)
    if floor_mhz > ceiling_mhz:
        floor_mhz = ceiling_mhz
    if volt_floor_mv > volt_ceiling_mv:
        volt_floor_mv = volt_ceiling_mv

    # 0. Enforce the envelope first.
    if f > ceiling_mhz:
        return ceiling_mhz, min(v, volt_ceiling_mv), f"above max {ceiling_mhz} MHz → cap"
    if v > volt_ceiling_mv:
        return f, volt_ceiling_mv, f"above max {volt_ceiling_mv} mV → cap"
    if f < floor_mhz:
        return floor_mhz, v, f"below floor {floor_mhz} MHz → raise"

    # 1. Hard safety cutoffs — back BOTH levers off at once.
    hard = None
    if chip_c is not None and chip_c >= chip_cutoff_c:
        hard = f"chip {chip_c:.0f}°C ≥ {chip_cutoff_c:.0f}"
    elif vr_c is not None and vr_c >= vr_cutoff_c:
        hard = f"VR {vr_c:.0f}°C ≥ {vr_cutoff_c:.0f}"
    elif power_cutoff_w and power_w is not None and power_w >= power_cutoff_w:
        hard = f"power {power_w:.0f}W ≥ {power_cutoff_w:.0f}W"
    elif vin_mv is not None and (vin_mv < vin_min_mv or vin_mv > vin_max_mv):
        hard = f"Vin {vin_mv:.0f}mV out of range"
    if hard is not None:
        nf = max(floor_mhz, f - step_down_mhz)
        nv = max(volt_floor_mv, v - step_volt_mv)
        if nf == f and nv == v:
            return f, v, f"safety: {hard} (at floor)"
        return nf, nv, f"safety: {hard} → back off"

    # 2. Temperature over the user's limit → co-move down the V/F curve.
    if temp_c is not None and temp_c > temp_high_c:
        nf = max(floor_mhz, f - step_down_mhz)
        nv = max(volt_floor_mv, v - step_volt_mv)
        if nf == f and nv == v:
            return f, v, "hold (at limit)"
        return nf, nv, f"temp {temp_c:.1f}°C > {temp_high_c:.0f}°C → back off"

    # 3. Instability → cure with voltage if there's thermal+power headroom,
    #    otherwise drop frequency (voltage maxed or too hot to add volts).
    if hashrate_invalid:
        power_ok = (
            not power_cutoff_w or power_w is None or power_w < power_cutoff_w * 0.92
        )
        temp_ok = temp_c is None or temp_c <= temp_high_c - 2
        if v < volt_ceiling_mv and power_ok and temp_ok:
            nv = min(volt_ceiling_mv, v + step_volt_mv)
            return f, nv, f"{instability_label} → +{nv - v} mV (cure)"
        nf = max(floor_mhz, f - step_down_mhz)
        if nf == f:
            return f, v, "hold (at floor)"
        return nf, v, f"{instability_label}, V maxed → -{f - nf} MHz"

    # 4. Valid and cool → push frequency up for more hashrate.
    if valid and temp_c is not None and temp_c < temp_low_c and f < ceiling_mhz:
        nf = min(ceiling_mhz, f + step_up_mhz)
        if nf != f:
            return nf, v, f"valid & {temp_c:.1f}°C < {temp_low_c:.0f}°C → +{nf - f} MHz"

    # 5. Hold — park (no NVS write).
    return f, v, "hold (within deadband)"


# ============================================================================
# Per-miner state
# ============================================================================

class _GuardianState:
    """Mutable per-miner state the loop carries between ticks."""

    __slots__ = (
        "prev_accepted",
        "prev_rejected",
        "last_commanded_freq",
        "last_change_ts",
        "last_reason",
        "last_ts",
        "last_temp_c",
        "last_reject_pct",
        "soft_ceiling",
        "prev_hw_errors",
        "last_hashrate",
    )

    def __init__(self) -> None:
        self.prev_accepted: int | None = None
        self.prev_rejected: int | None = None
        self.last_commanded_freq: int | None = None
        self.last_change_ts: float = 0.0
        self.last_reason: str | None = None
        self.last_ts: float = 0.0
        self.last_temp_c: float | None = None
        self.last_reject_pct: float | None = None
        # In-memory ceiling pinned below a frequency that proved unstable, so
        # recovery can't climb back into it. Never written to the DB; resets
        # when the miner drops out of the loop (offline/disabled).
        self.soft_ceiling: int | None = None
        # Previous ASIC error counter, for the per-interval delta surfaced in
        # the live readout.
        self.prev_hw_errors: int | None = None
        self.last_hashrate: float | None = None


def _reject_pct(
    state: _GuardianState, sample: MinerSample, min_shares: int
) -> float | None:
    """Rejected-share % over the interval = Δrejected / Δ(acc+rej) × 100, or None.

    Replaces the old errorCount/total HW% which was wrong on AxeOS: the
    hashrateMonitor ``total`` field is the *hashrate*, not a work counter, so
    dividing the cumulative error count by it produced absurd values (>100%).
    Rejected shares (``sharesRejected`` / ``sharesAccepted``) are genuine
    monotonic counters available on every AxeOS family, and their ratio sits
    in the right ballpark (well under 1% on a healthy miner).

    Computed as a *windowed* delta (instability shows up as a burst of fresh
    rejects), guarded by ``min_shares``: if too few shares landed in the
    interval the rate is statistically meaningless, so we return None (the
    caller then governs on VR alone this tick). Returns None on the first
    tick (no baseline) and on a counter reset (miner rebooted).

    Side effect: advances the stored baseline to the current counters.
    """
    acc = sample.accepted
    rej = sample.rejected
    prev_a = state.prev_accepted
    prev_r = state.prev_rejected

    pct: float | None = None
    if (
        acc is not None and rej is not None
        and prev_a is not None and prev_r is not None
        and acc >= prev_a and rej >= prev_r  # guard against counter resets
    ):
        d_acc = acc - prev_a
        d_rej = rej - prev_r
        d_tot = d_acc + d_rej
        if d_tot >= max(1, int(min_shares)):
            pct = (d_rej / d_tot) * 100.0

    # Advance the baseline (also resets cleanly after a detected reset).
    state.prev_accepted = acc
    state.prev_rejected = rej
    return pct


# ============================================================================
# Guardian controller (one slow loop for the whole fleet)
# ============================================================================

class GuardianController:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._states: dict[int, _GuardianState] = {}
        # Live status per miner, surfaced by the API/UI.
        self._status: dict[int, dict[str, Any]] = {}
        self.last_tick_ts: float = 0.0

    # ---- lifecycle (mirrors AutoFanController) ----

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="minerwatch-guardian")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    def status(self, miner_id: int) -> dict[str, Any] | None:
        return self._status.get(int(miner_id))

    def reset_miner(self, miner_id: int) -> None:
        """Drop a miner's in-memory governor state (soft ceiling, reject-rate
        baseline, settle timer) and its live readout.

        Called when the user changes the miner's Guardian config so a setting
        change re-probes *immediately* — otherwise the soft ceiling only clears
        on the next tick, and a quick disable→re-enable (within one interval)
        never clears it at all because the per-tick cleanup never runs while the
        miner is disabled.
        """
        self._states.pop(int(miner_id), None)
        self._status.pop(int(miner_id), None)

    # ---- main loop ----

    async def _run(self) -> None:
        cfg = get_config().guardian
        log.info(
            "Guardian started — interval=%ds VR>%.0f°C −%dMHz / reject%%>%.2f −%dMHz "
            "/ VR<%.0f°C +%dMHz, floor=%dMHz",
            cfg.interval_seconds, cfg.vr_high_c, cfg.step_down_vr_mhz,
            cfg.reject_pct_max, cfg.step_down_err_mhz,
            cfg.vr_low_c, cfg.step_up_mhz, cfg.frequency_floor_mhz,
        )
        from .poller import poller as _poller

        while not self._stop.is_set():
            try:
                if get_config().guardian.enabled:
                    await self._tick(_poller.last_results)
            except Exception:  # noqa: BLE001
                log.exception("guardian tick error")
            # Re-read the interval each loop so a settings change takes effect
            # without a restart.
            interval = max(30, int(get_config().guardian.interval_seconds))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                continue
        log.info("Guardian stopped")

    async def _tick(self, samples: dict[int, MinerSample]) -> None:
        self.last_tick_ts = time.time()
        cfg = get_config()
        gcfg = cfg.guardian
        miners = await db.list_miners(only_enabled=True)
        seen: set[int] = set()

        for miner in miners:
            miner_id = int(miner["id"])
            if not _coerce_bool(miner.get("guardian_enabled")):
                continue
            family = (miner.get("family") or "").lower()
            if family not in GUARDIAN_FAMILIES:
                continue
            sample = samples.get(miner_id)
            if sample is None or not sample.online:
                continue
            # Skip a miner that's deliberately in standby (AxeOS pause): its
            # ASIC is powered down and reads 0 H/s, which the governor would
            # otherwise read as instability and try to "cure" by raising
            # voltage on a chip that isn't even running. Leaving it out of
            # ``seen`` also drops its state, so it restarts from a fresh
            # baseline on resume.
            if sample.mining_paused:
                continue
            seen.add(miner_id)
            try:
                await self._govern_one(miner, sample, gcfg, cfg)
            except Exception:  # noqa: BLE001
                log.exception("guardian: miner=%s govern error", miner.get("name"))

        # Drop state for miners no longer governed/online so a returning miner
        # starts with a fresh reject-rate baseline instead of a stale delta.
        for mid in list(self._states):
            if mid not in seen:
                self._states.pop(mid, None)
        for mid in list(self._status):
            if mid not in seen:
                self._status.pop(mid, None)

    async def _govern_one(self, miner: dict, sample: MinerSample, gcfg, cfg) -> None:
        miner_id = int(miner["id"])
        state = self._states.get(miner_id)
        if state is None:
            state = _GuardianState()
            self._states[miner_id] = state

        # Current frequency: trust the live sample; fall back to what we last
        # commanded if the firmware didn't report it this poll.
        current_freq = (
            int(sample.frequency_mhz)
            if sample.frequency_mhz
            else state.last_commanded_freq
        )

        # Reject % over the interval (advances the baseline as a side effect).
        reject_pct = _reject_pct(state, sample, gcfg.reject_min_shares)

        # Which sensor governs frequency for this miner: VR (default) or the
        # ASIC chip. Both are reported by every AxeOS family (Nerd* inherits the
        # Bitaxe parser). An unset/unknown value behaves like "vr" — the legacy
        # behaviour. In chip mode the chip is also driven by the fan PID and the
        # 75°C watchdog, so the governor only bites once the fan is saturated.
        source = "chip" if str(miner.get("guardian_temp_source") or "").lower() == "chip" else "vr"
        source_label = "Chip" if source == "chip" else "VR"

        # Fetch recent averages over the configured window to avoid transient dips/spikes
        window = int(getattr(gcfg, "hashrate_average_window_seconds", 30))
        avg_metrics = {}
        if window > 0:
            try:
                avg_metrics = await db.get_recent_metrics_average(miner_id, window)
            except Exception:
                log.exception("guardian: failed to fetch recent averages for miner=%s", miner.get("name"))

        # Fallback to instantaneous values if not found or window is 0
        hashrate_ths = avg_metrics.get("hashrate_ths")
        if hashrate_ths is None:
            hashrate_ths = sample.hashrate_ths

        temp_chip_c = avg_metrics.get("temp_chip_c")
        if temp_chip_c is None:
            temp_chip_c = sample.temp_chip_c

        temp_vr_c = avg_metrics.get("temp_vr_c")
        if temp_vr_c is None:
            temp_vr_c = sample.temp_vr_c

        power_w = avg_metrics.get("power_w")
        if power_w is None:
            power_w = sample.power_w

        error_pct = avg_metrics.get("error_pct")
        if error_pct is None:
            error_pct = sample.error_pct

        temp_c = temp_chip_c if source == "chip" else temp_vr_c

        # Effective hashrate (TH/s) and the ASIC hardware-error counter — the
        # signals behind the regression brake. ``hashrate_ths`` is AxeOS's
        # reported (real) hashrate; ``hw_errors`` is the summed per-ASIC invalid-
        # nonce count, which climbs when an overclock starts producing garbage
        # (those bad nonces crater real hashrate but never reach the pool, so the
        # reject-% term stays blind to them).
        hw_errors = sample.hw_errors
        err_delta = None
        if (
            hw_errors is not None
            and state.prev_hw_errors is not None
            and hw_errors >= state.prev_hw_errors
        ):
            err_delta = hw_errors - state.prev_hw_errors
        if hw_errors is not None:
            state.prev_hw_errors = hw_errors
        tele = dict(
            hashrate_ths=hashrate_ths,
            asic_errors=hw_errors,
            asic_error_delta=err_delta,
        )

        # Always record the latest reading for the status endpoint, even if we
        # can't act this tick.
        now = time.time()
        state.last_ts = now
        state.last_temp_c = temp_c
        state.last_reject_pct = reject_pct
        state.last_hashrate = hashrate_ths

        if current_freq is None:
            # Can't govern without knowing the frequency.
            self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                          "no frequency reading", changed=False, source=source,
                          soft_ceiling=state.soft_ceiling, **tele)
            return

        # Resolve the per-miner ceiling/floor.
        #   ceiling = the user's "max frequency". If unset (Guardian enabled
        #   out-of-band without a cap), fall back to the current freq so the
        #   governor can only hold/back off, never push up past an unknown cap.
        ceiling = miner.get("guardian_max_freq_mhz")
        ceiling = int(ceiling) if ceiling else int(current_freq)
        floor = miner.get("guardian_freq_floor_mhz")
        floor = int(floor) if floor else int(gcfg.frequency_floor_mhz)

        # Resolve the temperature thresholds. Defaults come from GuardianCfg per
        # source AND family (the BitForge's TPS546 needs a hotter VR band than
        # the Bitaxe's regulator); a per-miner ``guardian_max_temp_c`` overrides
        # only the HIGH point, and the recovery (LOW) point is derived by
        # subtracting the same hysteresis deadband the defaults carry — so the
        # user sets one number.
        default_high, default_low = gcfg.temp_band(
            source, (miner.get("family") or "").lower()
        )
        max_temp = miner.get("guardian_max_temp_c")
        if max_temp:
            temp_high = float(max_temp)
            temp_low = temp_high - (default_high - default_low)
        else:
            temp_high, temp_low = default_high, default_low

        # Fold in any soft ceiling pinned after a past instability so the
        # recovery branch can't climb back into a frequency that proved unstable.
        # It lives in memory only and resets when the miner drops out of the loop.
        eff_ceiling = ceiling
        if state.soft_ceiling is not None:
            eff_ceiling = min(eff_ceiling, int(state.soft_ceiling))

        # Effective-hashrate validity (the benchmark idea): the *theoretical*
        # hashrate for this frequency is ``freq * cores / 1e6`` TH/s, with
        # ``cores = small_core_count * asic_count``. A point is valid only if the
        # measured hashrate is >= ``valid_pct`` of it. Below that, ASIC errors are
        # eating real work even though temp and pool-reject look fine → step down.
        # Frequency is pushed UP only when the point is valid (``allow_recovery``),
        # so the governor never chases frequency into instability. We skip the
        # test right after a change — AxeOS's hashrate EWMA lags and would misread
        # the settling as a drop (and then we also don't push up, to be safe).
        # Theoretical hashrate: prefer the firmware's own ``expectedHashrate``
        # (exact match with the AxeOS dashboard), fall back to freq * cores / 1e6.
        expected_ths = sample.expected_hashrate_ths
        if expected_ths is None:
            cores = None
            if sample.small_core_count and sample.asic_count:
                cores = int(sample.small_core_count) * int(sample.asic_count)
            expected_ths = (int(current_freq) * cores) / 1_000_000 if cores else None
        settled = (now - state.last_change_ts) >= max(0, int(gcfg.hashrate_settle_seconds))
        can_validate = (
            expected_ths is not None and hashrate_ths is not None and settled
        )
        valid_hr = bool(can_validate and hashrate_ths >= expected_ths * float(gcfg.valid_pct))
        error_high = (
            error_pct is not None
            and float(error_pct) > float(gcfg.error_pct_max)
        )
        # Either signal means "unstable": back off (frequency-only) or cure with
        # voltage (co-tuner). The ASIC error % climbs when pushing frequency at
        # fixed voltage, usually before the hashrate itself dips below valid_pct.
        hashrate_invalid = bool((can_validate and not valid_hr) or error_high)
        allow_up = bool(valid_hr and not error_high)
        instab_label = (
            "error % high" if (error_high and valid_hr) else "hashrate below theoretical"
        )
        tele["expected_ths"] = round(expected_ths, 2) if expected_ths is not None else None
        tele["valid"] = valid_hr if can_validate else None
        tele["error_pct"] = round(error_pct, 2) if error_pct is not None else None

        # ---- Phase 2: voltage co-tuner path (per-miner opt-in) ----
        # When the voltage lever is enabled (global master + per-miner opt-in)
        # and the core voltage is known, co-tune V and F via decide_point. Else
        # fall through to the frequency-only path below (Phase 1 behaviour).
        voltage_on = (
            bool(getattr(gcfg, "v2_voltage_enabled", False))
            and _coerce_bool(miner.get("guardian_voltage_enabled"))
            and (sample.voltage_set_mv is not None or sample.voltage_mv is not None)
        )
        if voltage_on:
            # Steer on the SET core voltage (coreVoltage), not the measured
            # coreVoltageActual — the latter droops under load, so using it makes
            # the steps chase a moving value and can make the cure fail to raise
            # the set point (and look like the voltage "dropping" on a freq up).
            cur_v = int(
                sample.voltage_set_mv if sample.voltage_set_mv else sample.voltage_mv
            )
            drv = driver_for_record({**miner, "timeout": cfg.polling.request_timeout})
            if drv.can_set_voltage and drv.can_set_frequency:
                power_cut = float(sample.max_power_w) if sample.max_power_w else (
                    float(getattr(gcfg, "power_cutoff_w", 0) or 0) or None
                )
                # 12 V boards need the 5 V-shaped Vin window rescaled —
                # see vin_band().
                vin_lo, vin_hi = vin_band(
                    sample.input_voltage_mv,
                    float(gcfg.vin_min_mv),
                    float(gcfg.vin_max_mv),
                )
                tf, tv, vreason = decide_point(
                    current_freq=int(current_freq),
                    current_volt=cur_v,
                    ceiling_mhz=eff_ceiling,
                    floor_mhz=floor,
                    volt_ceiling_mv=int(gcfg.v2_voltage_ceiling_mv),
                    volt_floor_mv=int(gcfg.v2_voltage_floor_mv),
                    temp_c=temp_c,
                    temp_high_c=temp_high,
                    temp_low_c=temp_low,
                    hashrate_invalid=hashrate_invalid,
                    valid=allow_up,
                    instability_label=instab_label,
                    chip_c=temp_chip_c,
                    chip_cutoff_c=float(gcfg.chip_cutoff_c),
                    vr_c=temp_vr_c,
                    vr_cutoff_c=float(gcfg.vr_cutoff_c),
                    power_w=power_w,
                    power_cutoff_w=power_cut,
                    vin_mv=sample.input_voltage_mv,
                    vin_min_mv=vin_lo,
                    vin_max_mv=vin_hi,
                    step_down_mhz=gcfg.step_down_vr_mhz,
                    step_up_mhz=gcfg.step_up_mhz,
                    step_volt_mv=int(gcfg.v2_voltage_step_mv),
                )
                tele["voltage_mv"] = cur_v
                tele["target_voltage_mv"] = tv
                if hashrate_invalid and tf < int(current_freq):
                    state.soft_ceiling = (
                        tf if state.soft_ceiling is None
                        else min(int(state.soft_ceiling), tf)
                    )
                changed = tf != int(current_freq) or tv != cur_v
                if not changed:
                    self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                                  vreason, changed=False, ceiling=eff_ceiling,
                                  floor=floor, source=source,
                                  soft_ceiling=state.soft_ceiling, **tele)
                    return
                cooldown = int(gcfg.cooldown_seconds or 0)
                if cooldown > 0 and (now - state.last_change_ts) < cooldown:
                    self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                                  f"cooldown ({vreason})", changed=False,
                                  ceiling=eff_ceiling, floor=floor, source=source,
                                  soft_ceiling=state.soft_ceiling, **tele)
                    return
                # Apply with safe ordering: raise V before F; lower F before V.
                ok = True
                try:
                    if tv > cur_v:
                        ok = await drv.set_voltage(int(tv))
                    if ok and tf != int(current_freq):
                        ok = await drv.set_frequency(int(tf))
                    if ok and tv < cur_v:
                        ok = await drv.set_voltage(int(tv))
                except Exception as exc:  # noqa: BLE001
                    log.warning("guardian: miner=%s co-tune failed: %s",
                                miner.get("name"), exc)
                    self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                                  f"co-tune failed: {exc}", changed=False,
                                  ceiling=eff_ceiling, floor=floor, source=source,
                                  soft_ceiling=state.soft_ceiling, **tele)
                    return
                if ok:
                    state.last_commanded_freq = int(tf)
                    state.last_change_ts = now
                    state.last_reason = vreason
                    tele["voltage_mv"] = int(tv)
                    log.info(
                        "guardian: miner=%s V/F %d→%d MHz %d→%d mV (%s) "
                        "[%s=%s hr=%s exp=%s ceiling=%d]",
                        miner.get("name"), int(current_freq), int(tf), cur_v, int(tv),
                        vreason, source_label,
                        f"{temp_c:.1f}" if temp_c is not None else "n/a",
                        f"{hashrate_ths:.2f}" if hashrate_ths is not None else "n/a",
                        tele.get("expected_ths"), eff_ceiling,
                    )
                    self._publish(miner_id, miner, tf, temp_c, reject_pct, vreason,
                                  changed=True, ceiling=eff_ceiling, floor=floor,
                                  source=source, soft_ceiling=state.soft_ceiling, **tele)
                return

        target, reason = decide_frequency(
            current_freq=int(current_freq),
            ceiling_mhz=eff_ceiling,
            floor_mhz=floor,
            temp_c=temp_c,
            hw_error_pct=reject_pct,
            temp_high_c=temp_high,
            temp_low_c=temp_low,
            hw_error_pct_max=gcfg.reject_pct_max,
            step_down_temp_mhz=gcfg.step_down_vr_mhz,
            step_down_err_mhz=gcfg.step_down_err_mhz,
            step_up_mhz=gcfg.step_up_mhz,
            source_label=source_label,
            hashrate_invalid=hashrate_invalid,
            step_down_hr_mhz=gcfg.step_down_hashrate_mhz,
            allow_recovery=allow_up,
            instability_label=instab_label,
        )

        # When the instability brake fires, pin a soft ceiling just below the
        # frequency that broke so recovery settles under it instead of hunting
        # back into the unstable point.
        if hashrate_invalid and target < int(current_freq):
            state.soft_ceiling = (
                int(target) if state.soft_ceiling is None
                else min(int(state.soft_ceiling), int(target))
            )

        if target == int(current_freq):
            # Nothing to do — don't touch the miner (no NVS write).
            self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                          reason, changed=False, ceiling=eff_ceiling, floor=floor,
                          source=source, soft_ceiling=state.soft_ceiling, **tele)
            return

        # Optional cooldown: enforce extra settle time between changes.
        cooldown = int(gcfg.cooldown_seconds or 0)
        if cooldown > 0 and (now - state.last_change_ts) < cooldown:
            self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                          f"cooldown ({reason})", changed=False,
                          ceiling=eff_ceiling, floor=floor, source=source,
                          soft_ceiling=state.soft_ceiling, **tele)
            return

        # Apply the change (live — no restart on AxeOS).
        drv = driver_for_record({**miner, "timeout": cfg.polling.request_timeout})
        if not drv.can_set_frequency:
            return
        try:
            ok = await drv.set_frequency(int(target))
        except Exception as exc:  # noqa: BLE001
            log.warning("guardian: miner=%s set_frequency failed: %s",
                        miner.get("name"), exc)
            self._publish(miner_id, miner, current_freq, temp_c, reject_pct,
                          f"set_frequency failed: {exc}", changed=False,
                          ceiling=eff_ceiling, floor=floor, source=source,
                          soft_ceiling=state.soft_ceiling, **tele)
            return
        if ok:
            state.last_commanded_freq = int(target)
            state.last_change_ts = now
            state.last_reason = reason
            log.info(
                "guardian: miner=%s %d→%d MHz (%s) [%s=%s reject%%=%s hr=%s "
                "ceiling=%d floor=%d]",
                miner.get("name"), int(current_freq), int(target), reason,
                source_label,
                f"{temp_c:.1f}" if temp_c is not None else "n/a",
                f"{reject_pct:.2f}" if reject_pct is not None else "n/a",
                f"{hashrate_ths:.2f}" if hashrate_ths is not None else "n/a",
                eff_ceiling, floor,
            )
            self._publish(miner_id, miner, target, temp_c, reject_pct, reason,
                          changed=True, ceiling=eff_ceiling, floor=floor,
                          source=source, soft_ceiling=state.soft_ceiling, **tele)
        else:
            log.warning("guardian: miner=%s rejected set_frequency(%d)",
                        miner.get("name"), int(target))

    def _publish(
        self,
        miner_id: int,
        miner: dict,
        freq: int | None,
        temp_c: float | None,
        reject_pct: float | None,
        reason: str,
        *,
        changed: bool,
        ceiling: int | None = None,
        floor: int | None = None,
        source: str = "vr",
        hashrate_ths: float | None = None,
        asic_errors: int | None = None,
        asic_error_delta: int | None = None,
        soft_ceiling: int | None = None,
        expected_ths: float | None = None,
        valid: bool | None = None,
        error_pct: float | None = None,
        voltage_mv: int | None = None,
        target_voltage_mv: int | None = None,
    ) -> None:
        """Update the live status surfaced by the API/UI.

        ``temp_c`` is the governed sensor's reading and ``source`` says which
        sensor it is ("vr" | "chip"), so the UI can label it correctly. The
        legacy ``vr_temp_c`` key is kept (populated only in VR mode) so any
        older consumer keeps working. ``hashrate_ths`` / ``asic_errors`` are the
        effective-hashrate and ASIC hardware-error readings the regression brake
        watches; ``soft_ceiling`` is the in-memory cap pinned after a regression
        (``ceiling`` already reflects it — this is for an explicit UI hint).
        """
        temp_r = round(temp_c, 1) if temp_c is not None else None
        self._status[miner_id] = {
            "miner_id": miner_id,
            "frequency_mhz": freq,
            "ceiling_mhz": ceiling,
            "floor_mhz": floor,
            "temp_c": temp_r,
            "temp_source": source,
            "vr_temp_c": temp_r if source == "vr" else None,
            "reject_pct": round(reject_pct, 3) if reject_pct is not None else None,
            "hashrate_ths": round(hashrate_ths, 2) if hashrate_ths is not None else None,
            "expected_ths": expected_ths,
            "valid": valid,
            "error_pct": error_pct,
            "voltage_mv": voltage_mv,
            "target_voltage_mv": target_voltage_mv,
            "asic_errors": asic_errors,
            "asic_error_delta": asic_error_delta,
            "soft_ceiling_mhz": soft_ceiling,
            "reason": reason,
            "changed": bool(changed),
            "ts": int(time.time()),
        }


def _coerce_bool(value: Any) -> bool:
    """SQLite stores the per-miner flag as 0/1; tolerate bool/str too."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


# Global instance (used by main.py)
guardian = GuardianController()

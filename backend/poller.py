# SPDX-License-Identifier: AGPL-3.0-only
"""Async miner-polling task.

On every tick we:
1. read the list of enabled miners from the DB
2. for each one, run the matching driver and collect the MinerSample
3. save a row in `metrics` and update `last_status`/`last_seen_ts`
4. invoke the alert system to evaluate thresholds/transitions

Drivers are stateless and polling is fully parallel (asyncio.gather),
so 4-10 miners are polled in <1s even with a 4s per-miner timeout.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from .config import get_config
from . import db, alerts, coin as coin_mod, coin_difficulty
from .log_streamer import log_streamer
from .miners import driver_for_record
from .miners.base import MinerSample
from .ambient_temp import ambient

log = logging.getLogger("minerwatch.poller")


class Poller:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_results: dict[int, MinerSample] = {}
        # Background refresh of the per-coin reference difficulties. Kept
        # off the poll path so a slow explorer can never delay a cycle;
        # the handle exists only so we don't stack up duplicate tasks.
        self._difficulty_task: asyncio.Task | None = None
        # Per-miner state for the hashrate EMA. We smooth server-side because:
        # - Bitaxe already exposes a ~1m firmware average (light smoothing ok)
        # - Braiins now reads GHS 1m (light smoothing ok)
        # - Canaan does not expose moving-window averages, so here we smooth
        #   `GHSspd`, which is instantaneous.
        # Auto-reset when a miner goes offline.
        self._hashrate_ema: dict[int, float] = {}

    @property
    def last_results(self) -> dict[int, MinerSample]:
        return self._last_results

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="minerwatch-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def poll_once(self) -> dict[int, MinerSample]:
        miners = await db.list_miners(only_enabled=True)
        if not miners:
            return {}

        async def poll_one(record: dict) -> tuple[int, MinerSample]:
            cfg = get_config()
            driver = driver_for_record({**record, "timeout": cfg.polling.request_timeout})
            try:
                sample = await driver.poll()
            except Exception as exc:  # noqa: BLE001
                log.warning("poll error for %s: %s", record["host"], exc)
                sample = MinerSample(
                    family=record["family"],
                    host=record["host"],
                    online=False,
                    error=str(exc),
                )
            return int(record["id"]), sample

        results = await asyncio.gather(*(poll_one(m) for m in miners))
        ts = int(time.time())
        cfg_now = get_config()
        # Build a tiny id→name map up-front (already in `miners` list)
        # so notify_new_alltime_best gets a friendly device name without
        # an extra DB roundtrip.
        names_by_id = {int(m["id"]): m.get("name") or m.get("host") for m in miners}
        # Full rows too: the block-found check needs each miner's
        # coin_override to resolve which chain to grade its best share on.
        miners_by_id = {int(m["id"]): m for m in miners}
        out: dict[int, MinerSample] = {}
        for miner_id, sample in results:
            self._smooth_hashrate(miner_id, sample, cfg_now)
            out[miner_id] = sample
            await db.update_miner_status(miner_id, "online" if sample.online else "offline")
            if sample.online:
                await db.insert_metric(miner_id, ts, sample.to_db_sample())
                # Feed the live streamer with what only REST can see on
                # firmware that logs no per-share lines (forge-os v1.5+):
                # stratumDiff seeds the synthetic-share chart floor.
                if log_streamer.is_supported(sample.family):
                    log_streamer.note_pool_target(
                        miner_id, (sample.raw or {}).get("stratumDiff")
                    )
                # Track session/all-time best-share. The helper handles
                # session-reset detection (uptime decreasing) and the
                # monotonic all-time max. No-op if best_difficulty is None.
                rec = await db.update_best_records(
                    miner_id,
                    sample.best_difficulty,
                    sample.uptime_s,
                    ts=ts,
                    # Bitaxe firmware persists its own all-time best in
                    # NVS (`bestDiff`). Threading it as an alltime_hint
                    # lets MinerWatch silently catch up to whatever the
                    # device already knew — no push, just truth.
                    alltime_hint=sample.best_difficulty_alltime,
                )
                # Push only on a fresh all-time record. Anti-spam guards
                # (first-ever seed, +0.1% growth, 60s cool-down) live in
                # alerts.notify_new_alltime_best.
                evt = rec.get("events", {}) if isinstance(rec, dict) else {}
                if evt.get("new_alltime"):
                    prev = evt.get("prev_alltime")
                    new = rec.get("alltime") or {}
                    await alerts.notify_new_alltime_best(
                        miner_id=miner_id,
                        miner_name=names_by_id.get(miner_id, sample.host),
                        prev_value=(prev["value"] if prev else None),
                        new_value=new.get("value"),
                        ts=ts,
                    )
                # A fresh session best is the only REST trace of a high
                # share's exact difficulty on per-share-less firmware:
                # upgrade the matching synthetic event and feed the Hall
                # of Fame. No-op for streams with a real share stream.
                if evt.get("new_session") and log_streamer.is_supported(sample.family):
                    sess_value = (rec.get("session") or {}).get("value")
                    if sess_value:
                        await log_streamer.note_session_best(
                            miner_id, float(sess_value), ts=ts
                        )

                # =====================================================
                # BLOCK-FOUND detection — see alerts.py for the opt-out.
                # To disable entirely, flip alerts.BLOCK_FOUND_ENABLED
                # to False; this branch will short-circuit immediately.
                # =====================================================
                if alerts.BLOCK_FOUND_ENABLED and sample.best_difficulty is not None:
                    # Prefer the network difficulty the miner itself is
                    # using (consistent with its share grading). Fall
                    # back to the cached/external value otherwise.
                    #
                    # The fallback must be the difficulty of the coin THIS
                    # miner is on: alerts.get_network_difficulty() only ever
                    # knows about Bitcoin, so on firmware that reports no
                    # stratum difficulty (Braiins, LuxOS, Canaan) a BCH miner
                    # used to be measured against the BTC target — roughly a
                    # thousand times too high, meaning a real BCH block would
                    # never have fired this alert. Resolve the coin first and
                    # use its own difficulty; fall through to the Bitcoin
                    # path only when the coin is genuinely unknown.
                    network_diff = None
                    if sample.network_difficulty:
                        network_diff = float(sample.network_difficulty)
                    else:
                        references = coin_difficulty.cached_references()
                        miner_coin, _ = coin_mod.classify(
                            miners_by_id.get(miner_id), sample, references
                        )
                        if miner_coin:
                            network_diff = references.get(miner_coin)
                    if network_diff is None:
                        network_diff = await alerts.get_network_difficulty()
                    if network_diff and sample.best_difficulty >= network_diff:
                        await alerts.notify_block_found(
                            miner_id=miner_id,
                            miner_name=names_by_id.get(miner_id, sample.host),
                            share_difficulty=float(sample.best_difficulty),
                            network_difficulty=float(network_diff),
                            ts=ts,
                        )

        # Evaluate alerts after we've saved everything
        await alerts.evaluate(out)

        # Persist the pushed ambient (room) temperature as a time-series —
        # one row per live sensor per cycle — so a miner History chart can
        # overlay the temperature of its assigned room. Only sensors with a
        # fresh reading are written: a stale or absent sensor simply writes
        # nothing, leaving a gap in its line rather than a flat zero.
        try:
            for amb in ambient.snapshot_all():
                # Keep the cached room name fresh on any miner using this
                # sensor (heals renames + assignments made before the cache).
                if amb.sensor_id and amb.name:
                    await db.refresh_assigned_sensor_name(amb.sensor_id, amb.name)
                if amb.available and amb.current_c is not None and amb.sensor_id:
                    await db.insert_ambient_metric(
                        amb.sensor_id, ts, round(float(amb.current_c), 1)
                    )
        except Exception:  # noqa: BLE001
            log.debug("ambient persist skipped", exc_info=True)

        self._last_results = out
        return out

    async def _run(self) -> None:
        cfg = get_config()
        log.info("Poller started, interval=%ss", cfg.polling.interval_seconds)
        while not self._stop.is_set():
            cycle_start = time.monotonic()
            try:
                self._warm_coin_difficulties()
                await self.poll_once()
                # Rollup runs ~every minute (cheap, idempotent), cleanup
                # runs ~every hour. Both are guarded by separate "last
                # ran" timestamps stored in the settings table so they
                # survive process restarts.
                await self._rollup_if_due()
                await self._cleanup_if_due()
            except Exception:  # noqa: BLE001
                log.exception("poller cycle error")

            elapsed = time.monotonic() - cycle_start
            wait = max(0.5, cfg.polling.interval_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
        log.info("Poller stopped")

    def _warm_coin_difficulties(self) -> None:
        """Kick off a background refresh of the per-coin difficulties.

        Fire-and-forget on purpose: coin classification and the block-found
        check both need a reference difficulty per coin, but neither is
        worth delaying a poll cycle for. The TTL cache inside
        ``coin_difficulty`` means the spawned task usually returns without
        touching the network, and the in-flight guard keeps a slow fetch
        from spawning a new task on every tick.
        """
        if self._difficulty_task and not self._difficulty_task.done():
            return
        self._difficulty_task = asyncio.create_task(
            coin_difficulty.warm_cache(), name="minerwatch-coin-difficulty"
        )

    def _smooth_hashrate(self, miner_id: int, sample: MinerSample, cfg) -> None:
        """Apply an EMA to the sample's hashrate (in-place).

        Formula: ``alpha = 1 - exp(-dt / tau)``, where dt is the polling
        interval and tau the desired time constant (configurable).
        Equivalent to a 1st-order low-pass filter. Per-miner state lives
        in ``self._hashrate_ema``, reset when the miner goes offline or
        when tau is 0 (disabled).

        Side effects: overwrites ``sample.hashrate_ths`` with the smoothed
        value. ``efficiency_w_per_ths`` is also recomputed since it derives
        directly from hashrate × power.
        """
        if not sample.online or sample.hashrate_ths is None:
            self._hashrate_ema.pop(miner_id, None)
            return

        tau = max(0, int(cfg.polling.hashrate_smoothing_seconds))
        if tau <= 0:
            # Smoothing disabled → pass the raw value through, no state
            self._hashrate_ema.pop(miner_id, None)
            return

        dt = max(1, int(cfg.polling.interval_seconds))
        alpha = 1.0 - math.exp(-dt / tau)
        # Safety clamp: with dt >> tau, alpha ≈ 1 (no smoothing).
        alpha = max(0.0, min(1.0, alpha))

        raw = float(sample.hashrate_ths)
        prev = self._hashrate_ema.get(miner_id)
        if prev is None or alpha >= 1.0:
            smoothed = raw
        else:
            smoothed = alpha * raw + (1.0 - alpha) * prev
        self._hashrate_ema[miner_id] = smoothed
        sample.hashrate_ths = round(smoothed, 4)

        # Recompute efficiency consistently with the smoothed value
        if sample.power_w and smoothed > 0:
            sample.efficiency_w_per_ths = round(sample.power_w / smoothed, 2)

    async def _rollup_if_due(self) -> None:
        """Aggregate raw samples into the 1m and 1h rollup tiers.

        Runs at most once per minute. The 1m rollup re-aggregates the
        last 5 minutes (idempotent INSERT OR REPLACE), the 1h rollup
        re-aggregates the last 2 hours from the 1m tier. Both are cheap
        — typically a handful of bucket rows per miner per call.
        """
        now = int(time.time())
        last_str = await db.get_setting("_last_rollup_ts", "0")
        try:
            last = int(last_str or 0)
        except ValueError:
            last = 0
        if now - last < 60:
            return
        await db.rollup_to_1m(now=now)
        await db.rollup_to_1h(now=now)
        # Same cadence for the fleet-wide ambient series.
        await db.rollup_ambient_to_1m(now=now)
        await db.rollup_ambient_to_1h(now=now)
        await db.set_setting("_last_rollup_ts", str(now))

    async def _cleanup_if_due(self) -> None:
        # Cleanup roughly once an hour
        now = int(time.time())
        last_str = await db.get_setting("_last_cleanup_ts", "0")
        try:
            last = int(last_str or 0)
        except ValueError:
            last = 0
        if now - last < 3600:
            return
        cfg = get_config()
        deleted = await db.cleanup_tiered(
            retention_raw_hours=cfg.storage.retention_raw_hours,
            retention_1m_days=cfg.storage.retention_1m_days,
            retention_1h_days=cfg.storage.retention_1h_days,
        )
        await db.set_setting("_last_cleanup_ts", str(now))
        total = sum(deleted.values())
        if total:
            log.info(
                "retention cleanup: raw=%d, 1m=%d, 1h=%d (total %d)",
                deleted["metrics"], deleted["metrics_1m"], deleted["metrics_1h"], total,
            )


# Global instance (used by main.py)
poller = Poller()

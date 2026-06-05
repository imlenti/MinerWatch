# SPDX-License-Identifier: AGPL-3.0-only
"""Donate-hashrate controller.

Temporarily repoints miners at solo.ckpool with the project's BTC
donation address, then restores their previous pool when the timer
expires or the user hits STOP.

Modeled on :class:`backend.auto_control.AutoFanController`: a single
asyncio loop that ticks slowly (donations last hours, so we don't need
to poll often) plus a one-shot boot catch-up that reverts anything whose
window elapsed while the process was down — the crash safety net.

The pre-donation pool config of every miner is snapshotted to
``donation_miners.prev_pool`` (JSON) before the switch, so revert is
faithful even across restarts.

See docs/donate-hashrate-design.md.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from . import db
from .config import get_config
from .miners import driver_for_record
from .miners.base import PoolConfig

log = logging.getLogger("minerwatch.donations")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Project donation address. Mirrors the constant the frontend renders in
# the Donations page. It's owned server-side: the client never supplies
# the address, so a donation can only ever pay this wallet. If you fork
# MinerWatch and want donations to go to *your* wallet, change this (and
# the copy in frontend-react/src/lib/donation.ts).
DONATION_BTC_ADDRESS = "bc1qexhamvrpclpr2skyyw3u8edm8kznnvt6zjudxu"
# Fixed worker name → all donors share one address on solo.ckpool, so the
# hashrate competing for a block aggregates per-address (one shared
# lottery, not many tiny ones).
DONATION_WORKER = "donations"

CKPOOL_SOLO_URL = "solo.ckpool.org"
CKPOOL_SOLO_PORT = 3333

# Duration guardrails (hours). MIN is small so people can test quickly.
MIN_DONATION_HOURS = 0.1   # ~6 minutes
MAX_DONATION_HOURS = 72.0
DEFAULT_DONATION_HOURS = 6.0

# How often the loop checks for elapsed donations. Coarse on purpose —
# keeps traffic off the miners (same philosophy as auto_control.py).
TICK_SECONDS = 30

# Child statuses that mean "still in flight / needs reverting".
_IN_FLIGHT = ("active", "unreachable")


def donation_worker_name() -> str:
    return f"{DONATION_BTC_ADDRESS}.{DONATION_WORKER}"


def donation_pool_config() -> PoolConfig:
    """The pool we repoint donated miners at: solo.ckpool with the project
    address as the worker. Password is ignored by ckpool."""
    return PoolConfig(
        url=CKPOOL_SOLO_URL,
        port=CKPOOL_SOLO_PORT,
        user=donation_worker_name(),
        password="x",
    )


def donation_pool_config_fallback() -> PoolConfig:
    """The donation pool written into the *fallback* slot only.

    Used when a miner is currently mining on its fallback stratum
    (``isUsingFallbackStratum=1``): repointing the primary slot would have
    no effect because AxeOS stays on the fallback after the restart.
    Leaving the primary fields ``None`` makes ``set_pool`` touch only the
    fallback slot, so the user's primary config is preserved and the revert
    (which restores the full snapshot) stays faithful.
    """
    return PoolConfig(
        fb_url=CKPOOL_SOLO_URL,
        fb_port=CKPOOL_SOLO_PORT,
        fb_user=donation_worker_name(),
        fb_password="x",
    )


def _driver_for(miner: dict[str, Any]):
    cfg = get_config()
    return driver_for_record({**miner, "timeout": cfg.polling.request_timeout})


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class DonationController:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---- lifecycle (mirrors AutoFanController) ----

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="minerwatch-donations")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        log.info("DonationController started — tick=%ds", TICK_SECONDS)
        while not self._stop.is_set():
            try:
                await self._tick()
            except Exception:  # noqa: BLE001
                log.exception("donation tick error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                continue
        log.info("DonationController stopped")

    async def catch_up_on_boot(self) -> None:
        """Run once at startup: revert any donation whose window already
        elapsed while the process was down. This is what makes auto-revert
        survive a crash/restart."""
        now = int(time.time())
        due = await db.donation_miners_due(now)
        if due:
            log.info(
                "donation boot catch-up: %d miner(s) overdue for revert", len(due)
            )
        for dm in due:
            await self.revert_miner(int(dm["id"]))

    async def _tick(self) -> None:
        now = int(time.time())
        for dm in await db.donation_miners_due(now):
            await self.revert_miner(int(dm["id"]))

    # ---- actions ----

    async def start_donation(
        self, miner_ids: list[int], hours: float
    ) -> dict[str, Any]:
        """Snapshot + switch the given miners to the donation pool.

        Returns a per-miner result so the UI can show what started and
        what was rejected. Only creates a donation row once at least one
        miner is eligible.
        """
        hours = max(MIN_DONATION_HOURS, min(MAX_DONATION_HOURS, float(hours)))
        ends_ts = int(time.time() + hours * 3600)
        busy = await db.active_donation_miner_ids()

        results: list[dict[str, Any]] = []
        # Each prepared entry also carries a per-miner donation target: a
        # miner already mining on its fallback slot is repointed there
        # rather than on the primary (see donation_pool_config_fallback).
        prepared: list[
            tuple[int, dict[str, Any], Any, PoolConfig, PoolConfig]
        ] = []

        # de-dupe while preserving order
        seen: set[int] = set()
        for mid in miner_ids:
            if mid in seen:
                continue
            seen.add(mid)

            miner = await db.get_miner(mid)
            if miner is None:
                results.append({"miner_id": mid, "status": "error", "error": "miner not found"})
                continue
            if mid in busy:
                results.append({"miner_id": mid, "status": "error", "error": "already donating"})
                continue
            drv = _driver_for(miner)
            if not drv.can_set_pool:
                results.append({
                    "miner_id": mid, "status": "unsupported",
                    "error": f"{miner['family']} does not support pool switching yet",
                })
                continue
            try:
                prev = await drv.read_pool_config()
            except Exception as exc:  # noqa: BLE001
                results.append({
                    "miner_id": mid, "status": "error",
                    "error": f"could not read current pool: {exc}",
                })
                continue

            # Repoint whichever slot the miner is actually mining on. If it
            # has failed over to its fallback, rewriting the primary slot
            # would never take effect (AxeOS stays on the fallback after the
            # restart), so target the fallback slot instead. Drivers without
            # active_slot() default to primary — i.e. today's behaviour.
            slot = "primary"
            slot_reader = getattr(drv, "active_slot", None)
            if slot_reader is not None:
                try:
                    slot = (await slot_reader()) or "primary"
                except Exception:  # noqa: BLE001
                    slot = "primary"
            target = (
                donation_pool_config_fallback()
                if slot == "fallback"
                else donation_pool_config()
            )
            prepared.append((mid, miner, drv, prev, target))

        if not prepared:
            return {"donation_id": None, "ends_ts": ends_ts, "miners": results}

        donation_id = await db.create_donation(ends_ts, donation_worker_name())
        now = int(time.time())

        for mid, miner, drv, prev, target in prepared:
            prev_json = prev.to_json()
            err: str | None = None
            try:
                ok = await drv.set_pool(target)
            except Exception as exc:  # noqa: BLE001
                ok = False
                err = str(exc)

            if ok:
                # The config changed (PATCH accepted) → donation is on,
                # even if the follow-up restart was flaky; revert will fix
                # it either way.
                await db.add_donation_miner(
                    donation_id, mid, prev_json, status="active", applied_ts=now
                )
                results.append({"miner_id": mid, "status": "active"})
                log.info(
                    "donation start miner=%s → %s (%.1fh)",
                    miner.get("name"), donation_worker_name(), hours,
                )
            else:
                # Nothing was changed on the miner → record the failure but
                # there's nothing to revert.
                await db.add_donation_miner(
                    donation_id, mid, prev_json, status="error",
                    last_error=err or "miner rejected the pool switch",
                )
                results.append({
                    "miner_id": mid, "status": "error",
                    "error": err or "miner rejected the pool switch",
                })
                log.warning(
                    "donation start FAILED miner=%s err=%s", miner.get("name"), err
                )

        await db.recompute_donation_status(donation_id)
        return {"donation_id": donation_id, "ends_ts": ends_ts, "miners": results}

    async def revert_miner(self, dm_id: int) -> bool:
        """Restore one donation_miner's pre-donation pool. On transient
        failure leave it 'unreachable' so the loop retries next tick."""
        dm = await db.get_donation_miner(dm_id)
        if not dm or dm["status"] not in _IN_FLIGHT:
            return False

        donation_id = int(dm["donation_id"])
        miner = await db.get_miner(int(dm["miner_id"]))
        now = int(time.time())

        if miner is None:
            # Miner was deleted — nothing left to restore; close it out.
            await db.mark_donation_miner(dm_id, status="reverted", reverted_ts=now)
            await db.recompute_donation_status(donation_id)
            return True

        drv = _driver_for(miner)
        if not drv.can_set_pool:
            await db.mark_donation_miner(
                dm_id, status="error", last_error="driver can no longer set pool"
            )
            await db.recompute_donation_status(donation_id)
            return False

        err: str | None = None
        try:
            prev = PoolConfig.from_json(dm["prev_pool"])
            ok = await drv.set_pool(prev)
        except Exception as exc:  # noqa: BLE001
            ok = False
            err = str(exc)

        if ok:
            await db.mark_donation_miner(dm_id, status="reverted", reverted_ts=now)
            log.info("donation revert ok miner=%s", miner.get("name"))
        else:
            # Transient: keep it in flight so the next tick retries.
            await db.mark_donation_miner(
                dm_id, status="unreachable",
                last_error=err or "could not reach miner to revert",
            )
            log.warning(
                "donation revert failed miner=%s err=%s — will retry",
                miner.get("name"), err,
            )
        await db.recompute_donation_status(donation_id)
        return ok

    async def revert_donation(self, donation_id: int) -> int:
        """Revert every in-flight miner in a donation (STOP all). Returns
        how many reverted successfully."""
        n = 0
        for c in await db.donation_miners_for(donation_id):
            if c["status"] in _IN_FLIGHT:
                if await self.revert_miner(int(c["id"])):
                    n += 1
        return n


# Global instance (used by main.py), same pattern as auto_fan / guardian.
donation_controller = DonationController()

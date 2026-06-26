# SPDX-License-Identifier: AGPL-3.0-only
"""Driver for the NerdOctaxe (NerdOCTAXE-Plus / NerdOCTAXE-Gamma).

The NerdOctaxe is an 8-ASIC home-miner from the Bitaxe family. The hardware
is from Patsch91 (`NerdOCTAXE-Plus` and `NerdOCTAXE-Gamma` repos); the
firmware is `shufps/ESP-Miner-NerdQAxePlus`, a fork of AxeOS adapted for
multi-chip boards.

The REST surface is *almost* the same as Bitaxe — same `/api/system/info`,
same PATCH-to-`/api/system` for control — but with extras that matter:

  - ``currentA``                PSU draw in Amps (float)
  - ``fanrpm2`` / ``fanspeed2`` second physical fan (NerdOctaxe has two)
  - ``duplicateHWNonces``       aggregate HW-error counter (no per-chip
                                error array exists in this firmware)
  - ``fallbackStratumURL`` /    secondary pool (the device supports a
    ``fallbackStratumPort`` /   primary + fallback pool config)
    ``fallbackStratumUser``
  - ``stratum.activePoolMode``  nested object that tells which pool is
    / ``stratum.usingFallback`` currently mining

So we inherit from BitaxeDriver to reuse the HTTP client, the
control endpoints (PATCH/restart/fan/freq/voltage), and the base
parser, then layer the NerdOctaxe-specific fields on top.

Reference: ``main/http_server/handler_system.cpp`` in
https://github.com/shufps/ESP-Miner-NerdQAxePlus
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import PoolSnapshot
from .bitaxe import BitaxeDriver, _coerce_flag, _opt_float, _opt_int


class NerdOctaxeDriver(BitaxeDriver):
    """NerdOctaxe (8-ASIC, dual-fan, dual-pool) driver.

    All control endpoints (`set_fan_speed`, `set_frequency`,
    `set_voltage`, `restart`, plus pool control `read_pool_config` /
    `set_pool`) are inherited unchanged from :class:`BitaxeDriver`: the
    firmware accepts the same PATCH payload. Only the read path differs.
    """

    family = "nerdoctaxe"
    # Same defaults as Bitaxe; firmware is served on port 80.
    # Control capabilities (including can_set_pool) mirror the parent —
    # the dual-pool firmware accepts the same stratum PATCH fields.
    #
    # The NerdQAxe/NerdOctaxe firmware (shufps fork) does NOT expose AxeOS's
    # /api/system/pause + /resume. Instead it has a dedicated
    # POST /api/system/shutdown (cuts ASIC + power stage, non-persistent) and
    # reports the state as ``shutdown`` in /api/system/info. So can_pause
    # stays off and we expose can_shutdown; resume is the inherited restart.
    can_pause = False
    can_shutdown = True

    async def shutdown(self) -> bool:
        """Stop hashing via the shufps ``POST /api/system/shutdown``.

        Powers down the ASIC and the power stage; the controller stays
        online and reports ``shutdown: true`` in /api/system/info. This
        firmware has no soft resume — bring it back with :meth:`restart`
        (inherited) or a power cycle. Non-persistent.
        """
        url = f"{self._base_url()}/api/system/shutdown"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.post(url)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    def _parse(self, data: dict[str, Any]):
        # Start from the Bitaxe parser so we inherit the established
        # field mappings (hashrate, temp, accepted/rejected, best
        # difficulty, etc.). Then patch in the NerdOctaxe extras.
        sample = super()._parse(data)
        # Override the family — BitaxeDriver._parse() stamps "bitaxe".
        sample.family = self.family

        # Deliberate-stop state. The shufps firmware reports it as ``shutdown``
        # (bool) in /api/system/info — not AxeOS's ``miningPaused`` (which the
        # parent looked for and didn't find). Map it onto the shared
        # ``mining_paused`` field so the frontend shows the Standby badge and
        # the guardian skips a stopped miner, exactly like Bitaxe.
        _sd = data.get("shutdown")
        sample.mining_paused = _coerce_flag(_sd) if _sd is not None else None

        # ---- PSU draw (Amps) ---------------------------------------
        # firmware emits both `current` (mA, int) and `currentA` (A, float).
        # Prefer the A-scaled field; fall back to mA / 1000 if missing.
        current_a = _opt_float(data.get("currentA"))
        if current_a is None:
            current_ma = _opt_float(data.get("current"))
            if current_ma is not None:
                current_a = round(current_ma / 1000.0, 3)
        sample.current_a = current_a

        # ---- Second fan --------------------------------------------
        # `fanrpm2` is 0 when getNumFans() == 1; treat that as "no
        # second fan" rather than "fan stopped". `fanCount` is the
        # authoritative flag for whether a real second fan exists.
        fan_count = _opt_int(data.get("fanCount"))
        fan_rpm_2 = _opt_int(data.get("fan2rpm") or data.get("fanrpm2"))
        fan_pct_2 = _opt_float(data.get("fanspeed2") or data.get("fan2speed"))
        # Only expose the second-fan readings if the firmware reports
        # an actual second fan. Otherwise we'd render a "Fan 2: 0 rpm"
        # tile that's just visual noise.
        has_second_fan = (fan_count is not None and fan_count > 1) or (fan_rpm_2 is not None and fan_rpm_2 > 0)
        if has_second_fan:
            sample.fan_rpm_2 = fan_rpm_2
            sample.fan_pct_2 = fan_pct_2

        # ---- Hardware errors ---------------------------------------
        # The closest analogue to "chip error rate" in the AxeOS-fork
        # firmware. Aggregate, not per-chip. Prefer `duplicateHWNonces`;
        # if it's absent, keep the `errorCount` sum the Bitaxe parent already
        # put in `hw_errors`. This is raw telemetry only — the Guardian governs
        # on the reject rate (sharesRejected/Accepted), not on this counter.
        dh = _opt_int(data.get("duplicateHWNonces"))
        if dh is not None:
            sample.hw_errors = dh
            # duplicateHWNonces has no matching work denominator, so clear
            # hw_total (a % from two unrelated counters would be wrong anyway).
            sample.hw_total = None

        # ---- Dual-pool config (read-only) --------------------------
        fb_url = data.get("fallbackStratumURL") or ""
        fb_port = data.get("fallbackStratumPort")
        fb_user = data.get("fallbackStratumUser") or ""
        if fb_url:
            if fb_port:
                sample.pool_url_fallback = f"{fb_url}:{fb_port}"
            else:
                sample.pool_url_fallback = fb_url
        # `worker_fallback` mirrors `worker` (which BitaxeDriver fills
        # from `stratumUser`) — empty string means "not configured".
        sample.worker_fallback = fb_user or None

        # Which pool is currently in use. The firmware emits a nested
        # `stratum` object whose shape varies across builds:
        #   * older single-fallback builds: `activePoolMode` as a string
        #     ("primary"/"fallback") and/or a `usingFallback` boolean.
        #   * newer builds: `activePoolMode` as an *integer* (a pool-mode
        #     enum, NOT a slot index), `poolMode` + `poolBalance`, and a
        #     `pools[]` array (entry 0 = primary, 1 = fallback) where
        #     `connected: true` marks each pool that's actively mining.
        #     In "balance" mode BOTH pools are connected at once.
        # The `connected` flags in `pools[]` are the firmware's own
        # ground truth, so we trust those over the enum: for the legacy
        # `pool_active` scalar we report "primary" when slot 0 is
        # connected (it takes precedence — it's what the dashboard card
        # shows as the pool being mined), else "fallback".
        stratum = data.get("stratum") if isinstance(data.get("stratum"), dict) else None
        stratum_pools = (
            stratum.get("pools")
            if stratum and isinstance(stratum.get("pools"), list)
            else None
        )
        if stratum:
            mode = stratum.get("activePoolMode")
            if isinstance(mode, str) and mode:
                # Older firmware: lowercase string identifier.
                sample.pool_active = mode.lower()
            elif isinstance(stratum.get("usingFallback"), bool):
                sample.pool_active = (
                    "fallback" if stratum["usingFallback"] else "primary"
                )
            elif stratum_pools:
                # Newer firmware: `activePoolMode` is an int enum we
                # can't map to a slot, so use the per-pool `connected`
                # flags. Primary (index 0) wins for the scalar.
                p0 = stratum_pools[0] if len(stratum_pools) >= 1 else None
                p1 = stratum_pools[1] if len(stratum_pools) >= 2 else None
                if isinstance(p0, dict) and p0.get("connected"):
                    sample.pool_active = "primary"
                elif isinstance(p1, dict) and p1.get("connected"):
                    sample.pool_active = "fallback"

        # ---- Structured pools list ---------------------------------
        # The Bitaxe parent already filled ``sample.pools`` with a
        # single primary entry. Rebuild it here so we can (a) add the
        # fallback slot, and (b) — when the firmware exposes the
        # ``stratum.pools[]`` array — use the *real per-pool* counters
        # and ping it carries, which is far better than attributing the
        # global totals to one slot.
        #
        # Each ``stratum.pools[i]`` entry carries: connected, accepted,
        # rejected, pingRtt (ms), pingLoss (%). It does NOT carry the
        # URL/user — those live in the flat ``stratumURL`` /
        # ``fallbackStratumURL`` fields the Bitaxe parser already
        # mapped onto ``sample.pool_url`` / ``pool_url_fallback``.
        #
        # When the array is absent (older firmware), we fall back to
        # the previous behaviour: attribute the global accepted/rejected
        # to whichever slot is active, and use the top-level
        # ``lastpingrtt`` as the active slot's ping. The other slot's
        # counters stay None — zero-ing them would falsely imply "0
        # rejected" when in truth the firmware just doesn't tell us.
        top_ping = _opt_float(data.get("lastpingrtt"))
        top_ping_loss = _opt_float(data.get("recentpingloss"))

        def _slot(
            url: str | None,
            user: str | None,
            slot_name: str,
            idx: int,
        ) -> PoolSnapshot | None:
            if not url:
                return None
            entry = (
                stratum_pools[idx]
                if stratum_pools and idx < len(stratum_pools)
                and isinstance(stratum_pools[idx], dict)
                else None
            )
            if entry is not None:
                # Rich path: per-pool counters + ping straight from the
                # firmware's pools array.
                return PoolSnapshot(
                    url=url,
                    user=user,
                    accepted=_opt_int(entry.get("accepted")),
                    rejected=_opt_int(entry.get("rejected")),
                    active=bool(entry.get("connected")),
                    slot=slot_name,
                    ping_ms=_opt_float(entry.get("pingRtt")),
                    ping_loss=_opt_float(entry.get("pingLoss")),
                )
            # Fallback path: global counters attributed to the active
            # slot only, ping from the miner-level lastpingrtt.
            is_active = (
                sample.pool_active != "fallback"
                if slot_name == "primary"
                else sample.pool_active == "fallback"
            )
            return PoolSnapshot(
                url=url,
                user=user,
                accepted=sample.accepted if is_active else None,
                rejected=sample.rejected if is_active else None,
                active=is_active,
                slot=slot_name,
                ping_ms=top_ping if is_active else None,
                ping_loss=top_ping_loss if is_active else None,
            )

        pools_list: list[PoolSnapshot] = []
        primary = _slot(sample.pool_url, sample.worker, "primary", 0)
        if primary is not None:
            pools_list.append(primary)
        fallback = _slot(
            sample.pool_url_fallback, sample.worker_fallback, "fallback", 1
        )
        if fallback is not None:
            pools_list.append(fallback)
        sample.pools = pools_list
        return sample

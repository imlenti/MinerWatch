# SPDX-License-Identifier: AGPL-3.0-only
"""Driver per Bitaxe e NerdQAxe.

Espongono una API REST/JSON molto pulita su porta 80:
- ``GET  /api/system/info``         current metrics + identity
- ``PATCH /api/system``              imposta frequency/coreVoltage/fanspeed/autofanspeed
- ``POST /api/system/restart``      restart
- ``POST /api/system/OTA``          (firmware OTA, non usato qui)

Documentazione di riferimento: https://github.com/skot/bitaxe
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import (
    MinerDriver,
    MinerSample,
    PoolConfig,
    PoolSnapshot,
    parse_si_difficulty as _parse_si_difficulty,
)


class BitaxeDriver(MinerDriver):
    family = "bitaxe"
    DEFAULT_PORT = 80
    can_set_fan = True
    can_set_frequency = True
    can_set_voltage = True
    can_restart = True
    can_set_pool = True

    def _base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def poll(self) -> MinerSample:
        url = f"{self._base_url()}/api/system/info"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.get(url)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return MinerSample(
                family=self.family,
                host=self.host,
                online=False,
                error=str(exc),
            )

        return self._parse(data)

    async def fetch_asic_info(self) -> dict[str, Any]:
        """Fetch ``/api/system/asic`` — the static ASIC / board identity.

        This endpoint carries ``deviceModel`` (the human-readable model
        name: "Gamma", "Supra", "SupraHex", "NerdQAxe++"…), along with
        ``asicCount`` and the frequency/voltage option lists. It is
        separate from ``/api/system/info`` (live metrics) and is the
        authoritative source for *which* device this is — see
        https://osmu.wiki/bitaxe/api.

        Best-effort: returns ``{}`` on any error, or on firmware too old
        to expose the endpoint, so callers can fall back to ``ASICModel``.
        """
        url = f"{self._base_url()}/api/system/asic"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.get(url)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _ths(hashrate_value: Any) -> float | None:
        """Converte l'hashrate Bitaxe (di solito GH/s in float) in TH/s."""
        if hashrate_value is None:
            return None
        try:
            ghs = float(hashrate_value)
        except (TypeError, ValueError):
            return None
        return round(ghs / 1000.0, 4)

    def _parse(self, data: dict[str, Any]) -> MinerSample:
        hashrate_ths = self._ths(data.get("hashRate"))
        power_w = _opt_float(data.get("power"))

        eff = None
        if hashrate_ths and power_w and hashrate_ths > 0:
            eff = round(power_w / hashrate_ths, 2)

        # Bitaxe exposes temp (chip sensor 1) and vrTemp (voltage
        # regulator). Multi-ASIC boards like the SupraHex (6× BM1368) add
        # a second on-board chip sensor as temp2; single-ASIC boards omit
        # it (or report the firmware's -1 "no sensor" sentinel). We treat
        # any non-positive reading as absent so the sentinel never poisons
        # the max() below or surfaces as a bogus 0 °C row.
        temp_s1 = _valid_temp(_opt_float(data.get("temp")))
        temp_s2 = _valid_temp(_opt_float(data.get("temp2")))
        # ``temp_chip_c`` is the hottest chip sensor, matching every other
        # multi-sensor driver (luxos/braiins/canaan all use max()). This is
        # the value the overheat alert and the auto-fan PID read, so on a
        # two-sensor board it must follow the hotter cluster — not sensor 1,
        # which on the SupraHex can read ~12 °C cooler than sensor 2.
        _chip_temps = [t for t in (temp_s1, temp_s2) if t is not None]
        temp_chip = round(max(_chip_temps), 1) if _chip_temps else None
        temp_vr = _opt_float(data.get("vrTemp"))

        # Frequenza in MHz, voltage in mV
        freq_mhz = _opt_float(data.get("frequency"))
        voltage_mv = _opt_float(data.get("coreVoltageActual") or data.get("coreVoltage"))

        fan_rpm = _opt_int(data.get("fanrpm"))
        fan_pct = _opt_float(data.get("fanspeed"))

        # ASIC count. Modern AxeOS reports it directly as ``asicCount`` in
        # /api/system/info, and that stays the authoritative source. But
        # some firmware/board combinations omit it from the live-info
        # endpoint (the Gamma in the field report exposes it only via the
        # separate /api/system/asic identity endpoint), which left the UI
        # showing "—". When it's missing we fall back to the per-ASIC
        # ``hashrateMonitor.asics`` array, whose length is the physical
        # ASIC count: a single-ASIC Gamma reports one entry, a 6× BM1368
        # SupraHex reports six. This mirrors how the canaan/braiins/luxos
        # drivers derive their counts from a per-unit array length. We only
        # ever use it as a fallback, so an explicit ``asicCount`` always
        # wins if the firmware does report one.
        asic_count = _opt_int(data.get("asicCount"))
        if asic_count is None:
            asic_count = _asics_from_monitor(data)

        accepted = _opt_int(data.get("sharesAccepted"))
        rejected = _opt_int(data.get("sharesRejected"))
        # AxeOS exposes both:
        # - bestSessionDiff: best share since the last reboot
        # - bestDiff:        all-time best, persisted in NVS flash
        # Modern firmwares (2.x+) ship them as SI strings ("4.29G",
        # "2.15M"); older firmwares as raw numbers. We use the SI parser
        # so both formats survive. We populate `best_difficulty` with
        # the *session* value (it's what other drivers also expose);
        # the all-time hint is exposed via `raw["bestDiff"]` and is
        # tracked DB-side in `best_records`.
        # `dict.get(key, default)` only uses the default when the key is
        # missing — not when it's present with a falsy value (e.g. None
        # or ""). Prefer session, but fall back to all-time when the
        # session field is unavailable in either way.
        _bsd = data.get("bestSessionDiff")
        _bd = data.get("bestDiff")
        best_diff_session = _parse_si_difficulty(_bsd if _bsd not in (None, "") else _bd)
        # `bestDiff` is the firmware's NVS-persisted all-time record.
        # We thread it through as `best_difficulty_alltime` so MinerWatch
        # can seed its own DB record on first contact (or after a wipe)
        # without waiting for a brand-new high share to materialise.
        best_diff_alltime = _parse_si_difficulty(_bd)

        # AxeOS reports the current Bitcoin network difficulty as seen
        # via stratum. Most modern firmwares expose ``networkDifficulty``
        # as a numeric value already in "raw" form (no SI suffix). If
        # for some reason it comes back as an SI string ("125.5T"), the
        # SI parser handles both. Used by MinerWatch to detect a "block
        # found" event (share difficulty >= network difficulty).
        network_diff = _parse_si_difficulty(data.get("networkDifficulty"))

        pool_url = data.get("stratumURL") or data.get("stratumUrl")
        if pool_url and data.get("stratumPort"):
            pool_url = f"{pool_url}:{data['stratumPort']}"
        worker = data.get("stratumUser")

        # Fallback (secondary) stratum. Modern AxeOS / ESP-Miner exposes a
        # fallback pool on *all* Bitaxe-class boards — not just the
        # NerdOctaxe — via ``fallbackStratumURL`` / ``fallbackStratumPort``
        # / ``fallbackStratumUser``, plus ``isUsingFallbackStratum`` (0/1)
        # which tells us which slot is *currently* mining.
        fallback_url = data.get("fallbackStratumURL") or data.get("fallbackStratumUrl")
        if fallback_url and data.get("fallbackStratumPort"):
            fallback_url = f"{fallback_url}:{data['fallbackStratumPort']}"
        worker_fallback = data.get("fallbackStratumUser") or None
        # Only honour the "using fallback" flag when a fallback endpoint is
        # actually configured. Guards against firmware that reports the
        # flag set with no fallback URL, which would otherwise leave the
        # miner with no slot marked active at all.
        using_fallback = _coerce_flag(data.get("isUsingFallbackStratum")) and bool(
            fallback_url
        )

        sample = MinerSample(
            family=self.family,
            host=self.host,
            online=True,
            mac=(data.get("macAddr") or data.get("macAddress") or "").upper() or None,
            model=data.get("ASICModel") or data.get("boardVersion"),
            hostname=data.get("hostname"),
            firmware_version=data.get("version") or data.get("firmwareVersion"),
            hashrate_ths=hashrate_ths,
            power_w=power_w,
            efficiency_w_per_ths=eff,
            temp_chip_c=temp_chip,
            temp_chip_2_c=temp_s2,
            temp_vr_c=temp_vr,
            fan_rpm=fan_rpm,
            fan_pct=fan_pct,
            frequency_mhz=freq_mhz,
            voltage_mv=voltage_mv,
            asic_count=asic_count,
            # Aggregate HW-error counter, summed across ASICs from the
            # hashrateMonitor block. Raw telemetry only — NOT used by the
            # Guardian (which governs on the reject rate instead; see the note
            # on _hw_total_from_monitor). NerdOctaxe overrides this with
            # `duplicateHWNonces`.
            hw_errors=_hw_errors_from_monitor(data),
            # NOTE: this is NOT a work denominator — see _hw_total_from_monitor.
            hw_total=_hw_total_from_monitor(data),
            uptime_s=_opt_int(data.get("uptimeSeconds")),
            accepted=accepted,
            rejected=rejected,
            best_difficulty=best_diff_session,
            best_difficulty_alltime=best_diff_alltime,
            network_difficulty=network_diff,
            pool_url=pool_url,
            worker=worker,
            pool_url_fallback=fallback_url or None,
            worker_fallback=worker_fallback,
            pool_active=(
                ("fallback" if using_fallback else "primary")
                if (pool_url or fallback_url)
                else None
            ),
            raw=data,
        )

        # Synthesise the structured pools list. AxeOS exposes a primary
        # stratum slot and (on modern firmware) a fallback slot; we emit
        # one ``PoolSnapshot`` per configured slot. ``active`` follows the
        # firmware's ``isUsingFallbackStratum`` flag so the Pools page
        # marks whichever pool is *actually* mining — not always the
        # primary. ``status`` is left None because AxeOS has no equivalent
        # of cgminer's Alive/Dead flag: if the miner answered
        # ``/api/system/info`` and we got here, the pool is at least
        # reachable, and the frontend renders an implicit health pill from
        # accepted vs rejected.
        #
        # The share counters (``sharesAccepted`` / ``sharesRejected``) are
        # miner-level fleet-totals — AxeOS doesn't break them down per
        # slot — so we attribute them to whichever slot is currently
        # active and leave the idle slot's counters None. Zero-ing the
        # idle slot would falsely imply "0 rejected" when the firmware
        # simply doesn't report a per-slot breakdown.
        #
        # ``responseTime`` is AxeOS's stratum round-trip latency in ms
        # (the "ping" shown in the AxeOS web UI). It's a single
        # miner-level value — attributed here to the active slot only.
        # The NerdQAxe fork reports None here and uses per-pool
        # ``pingRtt`` instead; :class:`NerdOctaxeDriver` overrides this.
        ping_ms = _opt_float(data.get("responseTime"))
        pools: list[PoolSnapshot] = []
        if pool_url:
            primary_active = not using_fallback
            pools.append(
                PoolSnapshot(
                    url=pool_url,
                    user=worker,
                    accepted=accepted if primary_active else None,
                    rejected=rejected if primary_active else None,
                    active=primary_active,
                    slot="primary",
                    ping_ms=ping_ms if primary_active else None,
                )
            )
        if fallback_url:
            pools.append(
                PoolSnapshot(
                    url=fallback_url,
                    user=worker_fallback,
                    accepted=accepted if using_fallback else None,
                    rejected=rejected if using_fallback else None,
                    active=using_fallback,
                    slot="fallback",
                    ping_ms=ping_ms if using_fallback else None,
                )
            )
        sample.pools = pools
        return sample

    # ---- Controlli ----

    async def _patch_system(self, payload: dict[str, Any]) -> bool:
        url = f"{self._base_url()}/api/system"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def set_fan_speed(self, percent: int) -> bool:
        percent = max(0, min(100, int(percent)))
        # autofanspeed=0 disattiva l'autofan, fanspeed imposta il duty.
        return await self._patch_system({"autofanspeed": 0, "fanspeed": percent})

    async def set_auto_fan(self, enabled: bool) -> bool:
        return await self._patch_system({"autofanspeed": 1 if enabled else 0})

    async def set_frequency(self, mhz: int) -> bool:
        return await self._patch_system({"frequency": int(mhz)})

    async def set_voltage(self, millivolts: int) -> bool:
        return await self._patch_system({"coreVoltage": int(millivolts)})

    async def restart(self) -> bool:
        url = f"{self._base_url()}/api/system/restart"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.post(url)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    # ---- Pool control (Donate hashrate) ----

    async def _system_info(self) -> dict[str, Any]:
        """GET /api/system/info. Mirrors the fetch inlined in poll(); kept
        as a small helper so read_pool_config() can reuse it."""
        url = f"{self._base_url()}/api/system/info"
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            resp = await cli.get(url)
            resp.raise_for_status()
            return resp.json()

    async def read_pool_config(self) -> PoolConfig:
        """Snapshot the current stratum config (primary + fallback).

        NOTE: AxeOS does NOT return ``stratumPassword`` in
        ``/api/system/info`` (it's write-only), so the password can't be
        captured. On restore we fall back to ``"x"`` — fine for solo /
        home pools, which ignore the worker password. This is the one
        field that can't round-trip faithfully on AxeOS.
        """
        data = await self._system_info()
        return PoolConfig(
            url=data.get("stratumURL") or data.get("stratumUrl"),
            port=_opt_int(data.get("stratumPort")),
            user=data.get("stratumUser"),
            password=None,  # not exposed by AxeOS — restored as "x"
            fb_url=data.get("fallbackStratumURL") or data.get("fallbackStratumUrl"),
            fb_port=_opt_int(data.get("fallbackStratumPort")),
            fb_user=data.get("fallbackStratumUser"),
            fb_password=None,
        )

    async def set_pool(self, config: PoolConfig) -> bool:
        """Repoint the primary (and, if present in the snapshot, fallback)
        stratum slot, then restart so AxeOS reconnects to the new pool."""
        payload: dict[str, Any] = {}
        if config.url is not None:
            host, port = _split_host_port(config.url, config.port)
            payload["stratumURL"] = host
            if port is not None:
                payload["stratumPort"] = port
            if config.user is not None:
                payload["stratumUser"] = config.user
            payload["stratumPassword"] = config.password or "x"
        # Restore the fallback slot too when the snapshot carried one, so
        # revert is faithful for users who had a custom backup pool.
        if config.fb_url is not None:
            fb_host, fb_port = _split_host_port(config.fb_url, config.fb_port)
            payload["fallbackStratumURL"] = fb_host
            if fb_port is not None:
                payload["fallbackStratumPort"] = fb_port
            if config.fb_user is not None:
                payload["fallbackStratumUser"] = config.fb_user
            payload["fallbackStratumPassword"] = config.fb_password or "x"

        ok = await self._patch_system(payload)
        if ok:
            # AxeOS only picks up a new stratum after a restart.
            await self.restart()
        return ok

    async def active_slot(self) -> str | None:
        """Report which stratum slot AxeOS is currently mining on.

        Reads ``isUsingFallbackStratum`` from ``/api/system/info`` and
        returns ``"fallback"`` when it's set *and* a fallback endpoint is
        configured (same guard as :meth:`_parse`), else ``"primary"``.
        Returns ``None`` only when the miner can't be read, so the caller
        falls back to its default (primary) rather than guessing.
        """
        try:
            data = await self._system_info()
        except Exception:  # noqa: BLE001
            return None
        fb_url = data.get("fallbackStratumURL") or data.get("fallbackStratumUrl")
        if _coerce_flag(data.get("isUsingFallbackStratum")) and fb_url:
            return "fallback"
        return "primary"


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _split_host_port(url: str, port: int | None) -> tuple[str, int | None]:
    """Return ``(host, port)`` for an AxeOS stratum field.

    AxeOS stores host and port in separate fields. ``read_pool_config``
    already keeps them apart, but be defensive: if a caller passes a
    combined ``host:port`` string and no explicit port, split it. Strips
    any ``stratum+tcp://`` scheme prefix that some configs carry.
    """
    if url is None:
        return url, port
    host = url
    if "://" in host:
        host = host.split("://", 1)[1]
    if port is None and host.count(":") == 1:
        h, _, p = host.partition(":")
        try:
            return h, int(p)
        except ValueError:
            return h, None
    return host, port


def _asics_from_monitor(data: dict[str, Any]) -> int | None:
    """Derive the ASIC count from the ``hashrateMonitor.asics`` array.

    AxeOS's ``hashrateMonitor`` block carries one entry per physical ASIC,
    each with its own ``total`` / ``domains`` / ``errorCount`` (the inner
    ``domains`` array is the per-chip voltage/hash domains, *not* a chip
    count — don't confuse the two). The length of the outer ``asics`` list
    is therefore the physical ASIC count.

    Returns None — never 0 — when the block is absent (older firmware),
    malformed, or an empty list, so the caller leaves ``asic_count`` unset
    and the UI keeps showing "—" instead of a misleading "0". The block's
    shape drifts across firmware versions (e.g. the Gamma omits the
    per-entry ``frequency`` the SupraHex includes), so we validate types
    defensively and only count list membership.
    """
    monitor = data.get("hashrateMonitor")
    if not isinstance(monitor, dict):
        return None
    asics = monitor.get("asics")
    if not isinstance(asics, list) or not asics:
        return None
    return len(asics)


def _hw_errors_from_monitor(data: dict[str, Any]) -> int | None:
    """Sum the per-ASIC ``errorCount`` from the ``hashrateMonitor`` block.

    AxeOS's ``hashrateMonitor.asics[]`` carries one entry per physical ASIC,
    each with an ``errorCount`` (invalid nonces returned by that ASIC). Kept as
    raw telemetry; the Guardian does NOT use it (its only available denominator,
    ``total``, turned out to be the hashrate, not a work counter — so a real
    error % can't be derived from these fields. The Guardian uses the reject
    rate instead).

    Returns the summed count, or ``None`` when the block is absent (older
    firmware) or no entry reports ``errorCount``.
    """
    monitor = data.get("hashrateMonitor")
    if not isinstance(monitor, dict):
        return None
    asics = monitor.get("asics")
    if not isinstance(asics, list) or not asics:
        return None
    total = 0
    found = False
    for entry in asics:
        if isinstance(entry, dict) and entry.get("errorCount") is not None:
            try:
                total += int(entry["errorCount"])
                found = True
            except (TypeError, ValueError):
                continue
    return total if found else None


def _hw_total_from_monitor(data: dict[str, Any]) -> int | None:
    """Sum the per-ASIC ``total`` work counter from ``hashrateMonitor``.

    IMPORTANT: despite the name, ``total`` is NOT a cumulative work counter —
    confirmed against a real AxeOS device (v2.13.1), each ASIC's ``total``
    equals that ASIC's *hashrate* in GH/s (it's the sum of the per-domain
    hashrates in ``domains``). So it cannot serve as a denominator for an error
    %: dividing the cumulative ``errorCount`` by it yields nonsense (>100%).
    This is why the Guardian governs on the reject rate, not on errorCount/total.
    Kept here only as raw telemetry. Returns None when the field is absent.
    """
    monitor = data.get("hashrateMonitor")
    if not isinstance(monitor, dict):
        return None
    asics = monitor.get("asics")
    if not isinstance(asics, list) or not asics:
        return None
    total = 0
    found = False
    for entry in asics:
        if isinstance(entry, dict) and entry.get("total") is not None:
            try:
                total += int(entry["total"])
                found = True
            except (TypeError, ValueError):
                continue
    return total if found else None


def _valid_temp(value: float | None) -> float | None:
    """Drop the firmware's "sensor absent" sentinel.

    AxeOS reports an unpopulated temperature sensor as ``-1`` (seen on
    ``boardTemp`` and on the second chip sensor of single-sensor boards).
    A genuine chip reading is always a positive Celsius value, so we treat
    anything ``<= 0`` as missing — this keeps the sentinel out of the
    ``max()`` aggregation and prevents a phantom "0 °C" row in the UI.
    """
    if value is None or value <= 0:
        return None
    return value


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _coerce_flag(value: Any) -> bool:
    """Interpret a firmware boolean-ish field as a Python bool.

    AxeOS reports ``isUsingFallbackStratum`` as an int (0/1) on most
    builds, but we tolerate bool and string forms ("0"/"1"/"true"/…)
    so the parser survives firmware quirks across versions.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False

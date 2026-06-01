# SPDX-License-Identifier: AGPL-3.0-only
"""Driver for the NMAxe series of home miners (NMAxe, NMAxeGamma, and NMQAxe++).

These devices run the open-source ESP-Miner-NMAxe firmware by NMminer1024,
which is a fork of ESP-Miner/AxeOS. The hardware features one or multiple
ASIC chips from the Bitmain BM1366 / BM1370 family:

  - **NMAxe**: 1x BM1366 ASIC, typical hashrate ~450-550 GH/s, single fan.
  - **NMAxeGamma**: 1x BM1370 ASIC, typical hashrate ~1.2-2.0 TH/s, single fan.
  - **NMQAxe++**: 4x BM1370 ASICs, typical hashrate ~4.8-7.3 TH/s, dual fans (case + vcore), 2.8" TFT.

API REST/JSON Endpoints on port 80:
  - ``GET  /api/system/info``         live telemetry (power, temps, hashrate, fans, stratum used)
  - ``POST /api/system/restart``      graceful soft reboot
  - ``GET  /api/setting/mining``      current target frequency, voltage and primary/fallback pool settings
  - ``PATCH /api/setting/mining``     sets target frequency, core voltage, and primary/fallback pools
  - ``GET  /api/setting/preference``  screen, backlight, led, and detailed fan auto/manual settings
  - ``PATCH /api/setting/preference`` sets fans speed, target temps, auto flags, and screen options

Reference documentation: https://github.com/NMminer1024/ESP-Miner-NMAxe
"""
from __future__ import annotations

from typing import Any

import httpx

from .base import MinerSample, PoolSnapshot, PoolConfig, parse_si_difficulty as _parse_si
from .bitaxe import BitaxeDriver, _opt_float, _opt_int


def _strip_scheme(url: str | None) -> str | None:
    """Drop a ``stratum+tcp://`` (or any) scheme prefix → bare ``host:port``."""
    if not url:
        return None
    if "://" in url:
        url = url.split("://", 1)[1]
    return url or None


def _split_stratum_url(full_url: str | None) -> tuple[str | None, int | None]:
    """Parses a full stratum URL into a separate host and integer port.

    Example: "stratum+tcp://solo.ckpool.org:3333" -> ("solo.ckpool.org", 3333)
    """
    if not full_url:
        return None, None

    url = full_url.strip()
    if "://" in url:
        _, url = url.split("://", 1)

    if ":" in url:
        host, _, port_str = url.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = None
        return host, port

    return url, None


def _make_full_url(url: str | None, port: int | None) -> str | None:
    """Combines host and port back into a full stratum URL with the correct scheme."""
    if not url:
        return None

    full_url = url.strip()

    # Strip any existing scheme from host first to prevent double prefixing
    if "://" in full_url:
        scheme, remainder = full_url.split("://", 1)
        scheme_prefix = f"{scheme}://"
        full_url = remainder
    else:
        scheme_prefix = "stratum+tcp://"

    # Append port if not already part of the URL string
    if ":" not in full_url and port is not None:
        full_url = f"{full_url}:{port}"

    return f"{scheme_prefix}{full_url}"


class NmaxeDriver(BitaxeDriver):
    """NMAxe / NMAxeGamma / NMQAxe++ — nested AxeOS-fork REST surface.

    ``poll()`` is inherited from :class:`BitaxeDriver` (same
    ``GET /api/system/info`` URL); only ``_parse`` and the control methods
    are overridden.
    """

    family = "nmaxe"
    DEFAULT_PORT = 80

    can_set_fan = True
    can_set_frequency = True
    can_set_voltage = True
    can_set_workmode = False
    can_restart = True
    can_pause = True
    can_shutdown = False
    can_set_pool = True

    async def fetch_probe(self) -> dict[str, Any]:
        """GET ``/probe`` — lightweight identity (model/hostname/version).

        Used by discovery to fingerprint the family: stock AxeOS / NerdQAxe
        have no ``/probe`` endpoint, so a 200 whose ``model`` starts with
        "NM" is a reliable NMAxe marker. Best-effort: ``{}`` on any error.
        """
        url = f"{self._base_url()}/probe"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.get(url)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _parse(self, data: dict[str, Any]) -> MinerSample:
        power = data.get("power") if isinstance(data.get("power"), dict) else {}
        temps = data.get("temps") if isinstance(data.get("temps"), dict) else {}
        asic = data.get("asic") if isinstance(data.get("asic"), dict) else {}
        miner = data.get("miner") if isinstance(data.get("miner"), dict) else {}
        identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
        stratum = data.get("stratum") if isinstance(data.get("stratum"), dict) else {}
        fans = data.get("fans") if isinstance(data.get("fans"), list) else []

        ghs = _opt_float(miner.get("hashRate"))
        hashrate_ths = round(ghs / 1000.0, 4) if ghs is not None else None

        power_w = _opt_float(power.get("power"))
        eff = None
        if hashrate_ths and power_w and hashrate_ths > 0:
            eff = round(power_w / hashrate_ths, 2)

        # PSU current: NMAxe reports input current in mA (`ibus`).
        ibus = _opt_float(power.get("ibus"))
        current_a = round(ibus / 1000.0, 3) if ibus is not None else None

        fan0 = fans[0] if len(fans) >= 1 and isinstance(fans[0], dict) else {}
        fan1 = fans[1] if len(fans) >= 2 and isinstance(fans[1], dict) else None

        pool_url = _strip_scheme(stratum.get("url"))
        worker = stratum.get("user") or None

        # `miner.paused` is a real bool on NMAxe → drives the Standby badge
        # and lets alerts skip a deliberately-stopped miner.
        paused = miner.get("paused")
        mining_paused = bool(paused) if isinstance(paused, bool) else None

        sample = MinerSample(
            family=self.family,
            host=self.host,
            online=True,
            mining_paused=mining_paused,
            mac=None,
            model=identity.get("hwModel") or identity.get("displayName"),
            chip_model=asic.get("model"),
            hostname=identity.get("hostName"),
            firmware_version=identity.get("fwVersion"),
            hashrate_ths=hashrate_ths,
            power_w=power_w,
            efficiency_w_per_ths=eff,
            temp_chip_c=_opt_float(temps.get("asic")),
            temp_vr_c=_opt_float(temps.get("vcore")),
            fan_rpm=_opt_int(fan0.get("rpm")),
            fan_pct=_opt_float(fan0.get("speed")),
            frequency_mhz=_opt_float(asic.get("freqReq")),
            voltage_mv=_opt_float(asic.get("vcoreReal")) or _opt_float(asic.get("vcoreReq")),
            voltage_set_mv=_opt_float(asic.get("vcoreReq")),
            asic_count=_opt_int(asic.get("count")),
            small_core_count=_opt_int(asic.get("smallCoreCnt")),
            input_voltage_mv=_opt_float(power.get("vbus")),
            current_a=current_a,
            uptime_s=_opt_int(miner.get("uptimeSeconds")),
            accepted=_opt_int(miner.get("sAccepted")),
            rejected=_opt_int(miner.get("sRejected")),
            best_difficulty=_parse_si(miner.get("bestDiffSession")),
            best_difficulty_alltime=_parse_si(miner.get("bestDiffEver")),
            network_difficulty=_parse_si(miner.get("networkDiff")),
            last_share_diff=_parse_si(miner.get("lastDiff")),
            pool_url=pool_url,
            worker=worker,
            raw=data,
        )

        # NMQAxe++ exposes a second (Vcore) fan as ``fans[1]``; single-fan
        # NMAxe / NMAxeGamma omit it, leaving these None.
        if fan1 is not None:
            sample.fan_rpm_2 = _opt_int(fan1.get("rpm"))
            sample.fan_pct_2 = _opt_float(fan1.get("speed"))

        # Single active-pool snapshot for the Pools page. NMAxe also has a
        # fallback slot, but only the *active* pool is in /api/system/info;
        # the full primary/fallback pair lives in /api/setting/mining and
        # would need a second request.
        if pool_url:
            sample.pools = [
                PoolSnapshot(
                    url=pool_url,
                    user=worker,
                    accepted=sample.accepted,
                    rejected=sample.rejected,
                    active=True,
                    slot="primary",
                )
            ]
        return sample

    # ---- Control API ----

    async def _patch_preference(self, payload: dict[str, Any]) -> bool:
        url = f"{self._base_url()}/api/setting/preference"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def set_fan_speed(self, percent: int) -> bool:
        """Manual fan duty via ``PATCH /api/setting/preference``.

        ``auto:false`` switches the fan out of firmware auto mode; ``id:0``
        is the ASIC fan on every NMAxe model (``id:1`` is the NMQAxe++
        Vcore fan, left on its own auto loop).
        """
        percent = max(0, min(100, int(percent)))
        return await self._patch_preference(
            {"fans": [{"id": 0, "auto": False, "speed": percent}]}
        )

    async def set_auto_fan(self, enabled: bool) -> bool:
        """Hand the ASIC fan back to the firmware's auto (target-temp) loop."""
        return await self._patch_preference(
            {"fans": [{"id": 0, "auto": bool(enabled)}]}
        )

    async def set_frequency(self, mhz: int) -> bool:
        url = f"{self._base_url()}/api/setting/mining"
        payload = {"asicFreqReq": int(mhz)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def set_voltage(self, millivolts: int) -> bool:
        url = f"{self._base_url()}/api/setting/mining"
        payload = {"asicVcoreReq": int(millivolts)}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def pause(self) -> bool:
        """Stop hashing via NMAxe ``PATCH /api/mining/state`` with paused=true."""
        url = f"{self._base_url()}/api/mining/state"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json={"paused": True})
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def resume(self) -> bool:
        """Resume hashing via NMAxe ``PATCH /api/mining/state`` with paused=false."""
        url = f"{self._base_url()}/api/mining/state"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json={"paused": False})
                resp.raise_for_status()
        except httpx.HTTPError:
            return False
        return True

    async def read_pool_config(self) -> PoolConfig:
        """Reads primary & fallback stratum config from /api/setting/mining."""
        url = f"{self._base_url()}/api/setting/mining"
        async with httpx.AsyncClient(timeout=self.timeout) as cli:
            resp = await cli.get(url)
            resp.raise_for_status()
            data = resp.json()

        stratum = data.get("stratum") or {}
        primary = stratum.get("primary") or {}
        fallback = stratum.get("fallback") or {}

        # Parse URLs (splitting scheme/host and port)
        p_host, p_port = _split_stratum_url(primary.get("url"))
        fb_host, fb_port = _split_stratum_url(fallback.get("url"))

        return PoolConfig(
            url=p_host,
            port=p_port,
            user=primary.get("user"),
            password=primary.get("pwd"),
            fb_url=fb_host,
            fb_port=fb_port,
            fb_user=fallback.get("user"),
            fb_password=fallback.get("pwd"),
        )

    async def set_pool(self, config: PoolConfig) -> bool:
        """Sets primary and (if present) fallback stratum configs, then restarts."""
        primary_url = _make_full_url(config.url, config.port)
        fallback_url = _make_full_url(config.fb_url, config.fb_port)

        payload: dict[str, Any] = {
            "stratum": {}
        }

        if primary_url:
            payload["stratum"]["primary"] = {
                "url": primary_url,
                "user": config.user or "",
                "pwd": config.password or "x",
            }

        if fallback_url:
            payload["stratum"]["fallback"] = {
                "url": fallback_url,
                "user": config.fb_user or "",
                "pwd": config.fb_password or "x",
            }

        url = f"{self._base_url()}/api/setting/mining"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as cli:
                resp = await cli.patch(url, json=payload)
                resp.raise_for_status()
        except httpx.HTTPError:
            return False

        # Apply requires a reboot
        await self.restart()
        return True

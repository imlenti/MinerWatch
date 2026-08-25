# SPDX-License-Identifier: AGPL-3.0-only
"""Driver for the original Canaan Avalon Nano 3 (not the Nano 3s).

The Nano 3 speaks the same cgminer-API on port 4028 as the Nano 3s, but
the dialect matches ``nano-cli`` (``avalon-nano-api``) rather than the
newer Avalon MM319 firmware:

  * performance preset is ``ascset|0,worklevel,{get,set}`` (0=low,
    1=med, 2=high) — not ``workmode``
  * reboot is ``ascset|0,reboot,all`` — not ``reboot,0``
  * no ``fan-spd`` / ``frequency`` / ``voltage`` write commands
  * ``version`` reports ``MODEL=nano3``, ``PROD=Avalonnano``,
    ``SWTYPE=MM318_…`` (Nano 3s is MM319 / MODEL nano3s)
  * original units often report ``DNA[0000000000000000]``; identity is
    MAC when the firmware exposes it, otherwise host

Poll reuses :class:`CanaanDriver` (``version`` + ``summary`` +
``estats`` + ``pools``). After the parent parse we:

  * pretty-print ``nano3`` / ``Avalonnano`` → ``Avalon Nano 3``
  * map ``WORKLEVEL`` onto ``sample.workmode`` so the existing Low/Mid/High
    UI lights up (the Nano 3 has no ``WORKMODE`` field)
  * fall back to ``WALLPOWER`` then ``PS[6]`` for watts when ``MPO`` is
    absent (Nano 3s-only)

Reference: ``nano-cli/internal/nano/nano.go`` and a real ``version``
payload (cgminer 4.11.1, API 3.7, MODEL nano3).
"""
from __future__ import annotations

from typing import Any

from .canaan import (
    CanaanDriver,
    _ascset_ok,
    _first_section,
    _opt_float,
    _opt_int,
    _parse_bracketed_fields,
)
from .cgminer_client import CgminerError


class CanaanNano3Driver(CanaanDriver):
    """Original Avalon Nano 3 — nano-cli dialect of the Canaan API."""

    family = "canaannano3"
    DEFAULT_PORT = 4028

    # nano-cli exposes worklevel + reboot only. Fan / frequency / voltage
    # writes are Nano 3s (and later) firmware; the original Nano 3 rejects
    # them, so we keep the flags off and let main.py hide those controls.
    can_set_fan = False
    can_set_frequency = False
    can_set_voltage = False
    can_set_workmode = True
    can_restart = True
    can_pause = False
    can_shutdown = False
    can_set_pool = False

    async def poll(self):
        sample = await super().poll()
        sample.family = self.family
        if not sample.online:
            return sample
        sample.model = _pretty_nano3_model(sample.model)

        fields = _mm_fields(sample)
        if fields:
            if sample.workmode is None:
                if (wl := _opt_int(fields.get("WORKLEVEL"))) is not None:
                    sample.workmode = wl
            if sample.power_w is None:
                sample.power_w = _nano3_power_w(fields)
            if sample.power_w and sample.hashrate_ths and sample.hashrate_ths > 0:
                sample.efficiency_w_per_ths = round(
                    sample.power_w / sample.hashrate_ths, 2
                )

        if sample.workmode is None:
            sample.workmode = await self._read_worklevel()
        return sample

    async def _read_worklevel(self) -> int | None:
        """``ascset|0,worklevel,get`` — nano-cli ``GetWorkLevel``.

        The cleaned cgminer Msg looks like ``worklevel 2`` (or just the
        digit). Returns 0/1/2, or None when the firmware doesn't answer.
        """
        try:
            resp = await self._client().call("ascset", "0,worklevel,get")
        except (CgminerError, OSError):
            return None
        return _parse_worklevel_response(resp)

    async def set_workmode(self, mode: int) -> bool:
        """Work level 0=low, 1=med, 2=high. nano-cli ``SetWorkLevel``."""
        if mode not in (0, 1, 2):
            return False
        try:
            resp = await self._client().call(
                "ascset", f"0,worklevel,set,{int(mode)}"
            )
        except CgminerError:
            return False
        return _ascset_ok(resp)

    async def restart(self) -> bool:
        """nano-cli ``Reboot``: ``ascset|0,reboot,all``."""
        try:
            resp = await self._client().call("ascset", "0,reboot,all")
        except CgminerError:
            return False
        return _ascset_ok(resp)


def _pretty_nano3_model(model: str | None) -> str:
    """Humanise the firmware MODEL/PROD strings.

    Real boards report ``MODEL=nano3`` and ``PROD=Avalonnano``; the parent
    Canaan parser prefers PROD, so we often see the latter.
    """
    raw = (model or "").strip()
    blob = raw.lower().replace(" ", "").replace("-", "").replace("_", "")
    if not blob or blob in ("nano3", "avalonnano", "avalonnano3"):
        return "Avalon Nano 3"
    if blob.startswith("nano3") and "3s" not in blob:
        return "Avalon Nano 3"
    return raw or "Avalon Nano 3"


def _mm_fields(sample) -> dict[str, str]:
    raw = sample.raw or {}
    stats = raw.get("stats") if isinstance(raw, dict) else None
    if not isinstance(stats, dict):
        return {}
    section = _first_section(stats, ("STATS",))
    if not section:
        return {}
    for key, value in section.items():
        if isinstance(key, str) and key.startswith("MM ID") and isinstance(value, str):
            return _parse_bracketed_fields(value)
    return {}


def _nano3_power_w(fields: dict[str, str]) -> float | None:
    """Watts when ``MPO`` is missing.

    Generic Avalon firmware exposes ``WALLPOWER``; some Nano builds put
    measured watts in ``PS[6]`` (same index pyasic uses on the Nano 3s).
    """
    if (wall := _opt_float(fields.get("WALLPOWER"))) is not None:
        return round(wall, 1)
    ps = fields.get("PS")
    if isinstance(ps, str) and ps.strip():
        parts = [_opt_float(x) for x in ps.split()]
        if len(parts) > 6 and parts[6] is not None:
            return round(parts[6], 1)
    return None


def _parse_worklevel_response(resp: dict[str, Any]) -> int | None:
    """Pull 0/1/2 out of an ``ascset worklevel,get`` reply."""
    msg = ""
    status = resp.get("STATUS") if isinstance(resp, dict) else None
    if isinstance(status, list) and status and isinstance(status[0], dict):
        msg = str(status[0].get("Msg") or status[0].get("Description") or "")
    elif isinstance(status, dict):
        msg = str(status.get("Msg") or status.get("Description") or "")
    elif isinstance(resp, dict):
        msg = str(resp.get("_raw_text") or "")
    tokens = msg.replace(",", " ").replace("=", " ").split()
    for tok in reversed(tokens):
        n = _opt_int(tok)
        if n in (0, 1, 2):
            return n
    return None

# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the original Avalon Nano 3 (canaannano3) driver.

The Nano 3 speaks the nano-cli dialect of the Canaan cgminer-API:
``worklevel`` (not ``workmode``), ``reboot,all``, and no fan / frequency
/ voltage writes. Discovery fingerprints ``MODEL=nano3`` so a Nano 3s
stays on the generic ``canaan`` driver.

Runs under pytest, or standalone: ``python tests/test_canaannano3.py``.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import discovery  # noqa: E402
from backend.discovery import _is_avalon_nano3, _nano3_marker  # noqa: E402
from backend.miners import DRIVERS, get_driver  # noqa: E402
from backend.miners.canaan import CanaanDriver  # noqa: E402
from backend.miners.canaannano3 import (  # noqa: E402
    CanaanNano3Driver,
    _parse_worklevel_response,
    _pretty_nano3_model,
)


# Real ``version`` payload from an original Nano 3 (cgminer 4.11.1).
_NANO3_VERSION = {
    "STATUS": [{"STATUS": "S", "Msg": "CGMiner versions", "Description": "cgminer 4.11.1"}],
    "VERSION": [{
        "CGMiner": "4.11.1",
        "API": "3.7",
        "PROD": "Avalonnano",
        "MODEL": "nano3",
        "HWTYPE": "PMMv1_X1",
        "SWTYPE": "MM318_X2",
        "VERSION": "24071801_42c628d",
        "HVERSION": "24071801_b906c52_6223725",
        "UPAPI": "2",
    }],
}

# Nano 3 estats: WORKLEVEL instead of WORKMODE, watts in PS[6], no MPO.
_NANO3_MM = (
    "Ver[Nano3-24071801] FW[Release] DNA[0000000000000000] "
    "SYSTEMSTATU[Work: In Work, Hash Board: 1] Elapsed[12345] "
    "HW[0] DH[1.200%] ITemp[-273] OTemp[32] TMax[58] TAvg[54] TarT[80] "
    "Fan1[2200] FanR[40%] PS[0 0 18000 4 0 5000 80] "
    "GHSspd[4100.00] GHSavg[4050.00] WU[56000.00] "
    "Freq[400] TA[4] WORKLEVEL[2]"
)

_NANO3_ESTATS = {"STATS": [{"MM ID0": _NANO3_MM}]}

_NANO3_SUMMARY = {
    "SUMMARY": [{
        "Elapsed": 12345,
        "MHS 5m": 4_050_000.0,
        "Accepted": 100,
        "Rejected": 2,
        "Best Share": 123456,
    }]
}

_NANO3_POOLS = {
    "POOLS": [{
        "POOL": 0,
        "URL": "stratum+tcp://solo.example.com:3333",
        "User": "bc1qxyz.nano3",
        "Status": "Alive",
        "Stratum Active": True,
        "Priority": 0,
        "Accepted": 100,
        "Rejected": 2,
    }]
}


def _cli_for(calls: dict[str, dict]) -> MagicMock:
    """Mock CgminerClient whose ``call(cmd, param=None)`` returns by command."""

    async def _call(command: str, parameter: str | None = None) -> dict:
        key = f"{command}|{parameter}" if parameter else command
        if key in calls:
            return calls[key]
        if command in calls:
            return calls[command]
        raise AssertionError(f"unexpected cgminer call: {command!r} {parameter!r}")

    cli = MagicMock()
    cli.call = AsyncMock(side_effect=_call)
    return cli


# ---- registry / capabilities ---------------------------------------


def test_registry_maps_family():
    assert get_driver("canaannano3") is CanaanNano3Driver
    assert DRIVERS["canaannano3"] is CanaanNano3Driver
    assert CanaanNano3Driver.family == "canaannano3"


def test_capabilities_worklevel_and_restart_only():
    assert CanaanNano3Driver.can_set_workmode is True
    assert CanaanNano3Driver.can_restart is True
    assert CanaanNano3Driver.can_set_fan is False
    assert CanaanNano3Driver.can_set_frequency is False
    assert CanaanNano3Driver.can_set_voltage is False
    assert CanaanNano3Driver.can_set_pool is False
    # The Nano 3s driver keeps the richer write surface.
    assert CanaanDriver.can_set_fan is True
    assert CanaanDriver.can_set_frequency is True


def test_pretty_model():
    assert _pretty_nano3_model("nano3") == "Avalon Nano 3"
    assert _pretty_nano3_model("Avalonnano") == "Avalon Nano 3"
    assert _pretty_nano3_model(None) == "Avalon Nano 3"
    assert _pretty_nano3_model("Avalon Q") == "Avalon Q"


# ---- poll ----------------------------------------------------------


def test_poll_maps_worklevel_power_and_model():
    cli = _cli_for({
        "version": _NANO3_VERSION,
        "summary": _NANO3_SUMMARY,
        "estats": _NANO3_ESTATS,
        "pools": _NANO3_POOLS,
    })
    drv = CanaanNano3Driver(host="10.0.0.50")
    with patch.object(drv, "_client", return_value=cli):
        sample = asyncio.run(drv.poll())

    assert sample.online is True
    assert sample.family == "canaannano3"
    assert sample.model == "Avalon Nano 3"
    assert sample.firmware_version == "24071801_42c628d"
    assert sample.workmode == 2
    assert sample.power_w == 80.0
    assert sample.temp_chip_c == 58.0
    assert sample.temp_avg_c == 54.0
    assert sample.temp_outlet_c == 32.0
    assert sample.fan_rpm == 2200
    assert sample.hashrate_ths == 4.05  # MHS 5m / 1e6
    assert sample.pool_url == "stratum+tcp://solo.example.com:3333"
    assert sample.worker == "bc1qxyz.nano3"
    # WORKLEVEL was in estats — no extra worklevel,get round-trip.
    cmds = [c.args[0] for c in cli.call.await_args_list]
    assert "ascset" not in cmds


def test_poll_offline_skips_worklevel_get():
    from backend.miners.cgminer_client import CgminerError

    cli = MagicMock()
    cli.call = AsyncMock(side_effect=CgminerError("connect failed"))
    drv = CanaanNano3Driver(host="10.0.0.50")
    with patch.object(drv, "_client", return_value=cli):
        sample = asyncio.run(drv.poll())
    assert sample.online is False
    assert sample.family == "canaannano3"
    # Parent poll tries ``version`` and returns; we must not add worklevel,get.
    assert cli.call.await_count == 1
    assert cli.call.await_args.args[0] == "version"


def test_poll_worklevel_get_fallback():
    """When estats has no WORKLEVEL, poll asks ``worklevel,get``."""
    mm = _NANO3_MM.replace(" WORKLEVEL[2]", "")
    cli = _cli_for({
        "version": _NANO3_VERSION,
        "summary": _NANO3_SUMMARY,
        "estats": {"STATS": [{"MM ID0": mm}]},
        "pools": _NANO3_POOLS,
        "ascset|0,worklevel,get": {
            "STATUS": [{"STATUS": "S", "Msg": "ASC 0 set worklevel 1"}],
        },
    })
    drv = CanaanNano3Driver(host="10.0.0.50")
    with patch.object(drv, "_client", return_value=cli):
        sample = asyncio.run(drv.poll())
    assert sample.workmode == 1


def test_parse_worklevel_response():
    assert _parse_worklevel_response(
        {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set worklevel 2"}]}
    ) == 2
    assert _parse_worklevel_response(
        {"STATUS": [{"STATUS": "S", "Msg": "0"}]}
    ) == 0
    assert _parse_worklevel_response({"STATUS": [{"STATUS": "S", "Msg": "nope"}]}) is None


# ---- writes (nano-cli commands) ------------------------------------


def test_set_workmode_sends_worklevel():
    cli = _cli_for({
        "ascset|0,worklevel,set,1": {"STATUS": [{"STATUS": "S", "Msg": "ASC 0 set OK"}]},
    })
    drv = CanaanNano3Driver(host="10.0.0.50")
    with patch.object(drv, "_client", return_value=cli):
        ok = asyncio.run(drv.set_workmode(1))
    assert ok is True
    cli.call.assert_awaited_once_with("ascset", "0,worklevel,set,1")


def test_set_workmode_rejects_out_of_range():
    drv = CanaanNano3Driver(host="10.0.0.50")
    assert asyncio.run(drv.set_workmode(3)) is False
    assert asyncio.run(drv.set_workmode(-1)) is False


def test_restart_sends_reboot_all():
    cli = _cli_for({
        "ascset|0,reboot,all": {"STATUS": [{"STATUS": "S", "Msg": "OK"}]},
    })
    drv = CanaanNano3Driver(host="10.0.0.50")
    with patch.object(drv, "_client", return_value=cli):
        ok = asyncio.run(drv.restart())
    assert ok is True
    cli.call.assert_awaited_once_with("ascset", "0,reboot,all")


# ---- discovery fingerprint -----------------------------------------


def test_is_avalon_nano3_real_payload():
    assert _is_avalon_nano3(_NANO3_VERSION["VERSION"][0]) is True


def test_is_avalon_nano3_rejects_nano3s():
    assert _is_avalon_nano3({"MODEL": "nano3s", "SWTYPE": "MM319"}) is False
    assert _is_avalon_nano3({"MODEL": "Nano 3s"}) is False
    assert _is_avalon_nano3({"MODEL": "Avalon Q"}) is False


def test_is_avalon_nano3_mm318_without_model():
    assert _is_avalon_nano3({"SWTYPE": "MM318_X2"}) is True
    assert _is_avalon_nano3({"SWTYPE": "MM319"}) is False


def test_fingerprint_nano3_not_canaan():
    with patch.object(
        discovery.CgminerClient, "call",
        AsyncMock(return_value=_NANO3_VERSION),
    ):
        family = asyncio.run(discovery._cgminer_fingerprint("10.0.0.50"))
    assert family == "canaannano3"


def test_fingerprint_nano3s_stays_canaan():
    version = {
        "VERSION": [{"CGMiner": "4.11.1", "MODEL": "nano3s", "SWTYPE": "MM319"}],
    }
    with patch.object(discovery.CgminerClient, "call", AsyncMock(return_value=version)):
        family = asyncio.run(discovery._cgminer_fingerprint("10.0.0.51"))
    assert family == "canaan"


def test_nano3_marker_live_ver_string():
    assert _nano3_marker("Ver[nano3-24071801_42c628d]") is True
    assert _nano3_marker("Ver[Nano3s-25021401]") is False
    assert _nano3_marker("Avalonnano") is None


def test_fingerprint_estats_ver_when_version_omits_model():
    """Some Avalon builds only stamp the model in estats Ver[]."""
    version = {"VERSION": [{"CGMiner": "4.11.1", "API": "3.7"}]}
    estats = {
        "STATS": [{
            "MM ID0": "Ver[nano3-24071801_42c628d] WORKLEVEL[0] MPO[60]",
        }]
    }

    async def _call(command: str, parameter: str | None = None):
        if command == "version":
            return version
        if command == "estats":
            return estats
        raise AssertionError(command)

    with patch.object(discovery.CgminerClient, "call", AsyncMock(side_effect=_call)):
        family = asyncio.run(discovery._cgminer_fingerprint("10.0.0.50"))
    assert family == "canaannano3"


def test_identify_cgminer_uses_nano3_driver():
    from backend.miners.base import MinerSample

    live = MinerSample(
        family="canaannano3", host="10.0.0.50", online=True,
        model="Avalon Nano 3", mac=None,
    )
    with patch.object(discovery, "_cgminer_fingerprint", AsyncMock(return_value="canaannano3")), \
         patch.object(CanaanNano3Driver, "poll", AsyncMock(return_value=live)):
        info = asyncio.run(discovery._identify_cgminer("10.0.0.50"))
    assert info is not None
    assert info["family"] == "canaannano3"
    assert info["model"] == "Avalon Nano 3"
    assert info["port"] == discovery.PORT_CGMINER


if __name__ == "__main__":
    fns = {k: v for k, v in dict(globals()).items() if k.startswith("test_")}
    for name, fn in fns.items():
        fn()
        print(f"ok  {name}")
    print(f"\n{len(fns)} canaannano3 tests passed")

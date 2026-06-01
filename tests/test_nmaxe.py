# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the NMAxe miner family driver (telemetry, fan control, discovery, WS)."""
from __future__ import annotations

import asyncio
import pathlib
import sys
from unittest.mock import AsyncMock, Mock, patch
import pytest

# Make the repo root importable whether invoked via pytest or directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from backend import discovery
from backend.log_streamer import LogStreamer, MinerStream
from backend.miners import DRIVERS, get_driver
from backend.miners.base import MinerSample, PoolSnapshot, PoolConfig
from backend.miners.nmaxe import NmaxeDriver


def _nmaxe_info(**overrides):
    """A realistic NMAxe v3.0.21 /api/system/info response payload."""
    data = {
        "power": {
            "power": 23.51316071,
            "vbus": 11370,
            "ibus": 2068,
        },
        "temps": {
            "vcore": 66.6015625,
            "asic": 56.8828125,
        },
        "asic": {
            "count": 1,
            "model": "BM1366",
            "vcoreReq": 1350,
            "vcoreReal": 1296,
            "freqReq": 675,
            "smallCoreCnt": 894,
        },
        "miner": {
            "state": "running",
            "paused": False,
            "pauseReason": "",
            "hashRate": 629.2,
            "bestDiffEver": "474.7M",
            "bestDiffSession": "2.874M",
            "networkDiff": "234.5M",
            "poolDiff": "65.5K",
            "lastDiff": "5.789K",
            "blkhits": 0,
            "freeHeap": 178000,
            "minFreeHeap": 162000,
            "sAccepted": 1464,
            "sRejected": 36,
            "uptimeSeconds": 17604,
            "uptimeEver": 348293,
        },
        "identity": {
            "fwVersion": "v3.0.21",
            "hwModel": "NMAxe",
            "displayName": "NMAxe",
            "hostName": "NMAxe_6b784",
            "ssid": "example-wifi",
            "rssi": -71,
        },
        "stratum": {
            "url": "stratum+tcp://digi.hmpool.io:3334",
            "user": "bc1qxyz.worker1",
            "pwd": "x",
        },
        "fans": [{"id": 0, "speed": 0, "rpm": 2420}],
    }
    data.update(overrides)
    return data


def _parse(**overrides):
    return NmaxeDriver("10.0.0.9")._parse(_nmaxe_info(**overrides))


# ---- registry ------------------------------------------------------


def test_family_registered():
    assert DRIVERS["nmaxe"] is NmaxeDriver
    assert get_driver("NMAxe") is NmaxeDriver


def test_minersample_has_last_share_diff_default_none():
    assert MinerSample(family="x", host="h").last_share_diff is None


# ---- _parse: nested NMAxe field dialect ----------------------------


def test_parse_core_fields_and_family():
    s = _parse()
    assert s.family == "nmaxe"
    assert s.online is True
    assert s.mac is None
    assert s.model == "NMAxe"
    assert s.chip_model == "BM1366"
    assert s.firmware_version == "v3.0.21"
    assert s.hostname == "NMAxe_6b784"
    assert s.hashrate_ths == 0.6292
    assert s.power_w == 23.51316071
    assert s.asic_count == 1
    assert s.small_core_count == 894
    assert s.uptime_s == 17604


def test_parse_thermal_and_voltage():
    s = _parse()
    assert round(s.temp_chip_c, 2) == 56.88
    assert round(s.temp_vr_c, 2) == 66.60
    assert s.voltage_set_mv == 1350
    assert s.voltage_mv == 1296
    assert s.frequency_mhz == 675
    assert s.input_voltage_mv == 11370
    assert s.current_a == 2.068


def test_parse_shares_and_difficulties():
    s = _parse()
    assert s.accepted == 1464
    assert s.rejected == 36
    assert round(s.best_difficulty) == 2_874_000
    assert round(s.best_difficulty_alltime) == 474_700_000
    assert round(s.network_difficulty) == 234_500_000
    # B-base for the Halo: last submitted share's difficulty from the poll.
    assert round(s.last_share_diff) == 5_789


def test_parse_pause_state_and_pool():
    s = _parse()
    assert s.mining_paused is False
    assert s.pool_url == "digi.hmpool.io:3334"
    assert s.worker == "bc1qxyz.worker1"
    assert len(s.pools) == 1
    assert s.pools[0].slot == "primary"
    assert s.pools[0].active is True
    assert s.pools[0].url == "digi.hmpool.io:3334"


def test_parse_efficiency():
    s = _parse()
    # 23.513 W / 0.6292 TH/s ~= 37.37 W/TH
    assert s.efficiency_w_per_ths == round(23.51316071 / 0.6292, 2)


def test_parse_single_fan():
    s = _parse()
    assert s.fan_rpm == 2420
    assert s.fan_pct == 0
    assert s.fan_rpm_2 is None
    assert s.fan_pct_2 is None


def test_parse_nmqaxe_second_fan():
    """NMQAxe++ exposes a second (Vcore) fan as fans[1]."""
    s = _parse(fans=[{"id": 0, "speed": 60, "rpm": 3600}, {"id": 1, "speed": 80, "rpm": 4200}])
    assert s.fan_rpm == 3600
    assert s.fan_pct == 60
    assert s.fan_rpm_2 == 4200
    assert s.fan_pct_2 == 80


def test_parse_paused_unknown_when_absent():
    info = _nmaxe_info()
    del info["miner"]["paused"]
    s = NmaxeDriver("10.0.0.9")._parse(info)
    assert s.mining_paused is None


# ---- capabilities --------------------------------------------------


def test_capabilities_supported():
    assert NmaxeDriver.can_set_fan is True
    assert NmaxeDriver.can_restart is True
    assert NmaxeDriver.can_set_frequency is True
    assert NmaxeDriver.can_set_voltage is True
    assert NmaxeDriver.can_set_workmode is False
    assert NmaxeDriver.can_pause is True
    assert NmaxeDriver.can_shutdown is False
    assert NmaxeDriver.can_set_pool is True


# ---- fan control: PATCH /api/setting/preference --------------------


def test_set_fan_speed_clamped_and_payload():
    drv = NmaxeDriver("10.0.0.9")
    with patch.object(NmaxeDriver, "_patch_preference", AsyncMock(return_value=True)) as pp:
        ok = asyncio.run(drv.set_fan_speed(150))
    assert ok is True
    assert pp.await_args.args[0] == {"fans": [{"id": 0, "auto": False, "speed": 100}]}


def test_set_auto_fan_payload():
    drv = NmaxeDriver("10.0.0.9")
    with patch.object(NmaxeDriver, "_patch_preference", AsyncMock(return_value=True)) as pp:
        ok = asyncio.run(drv.set_auto_fan(True))
    assert ok is True
    assert pp.await_args.args[0] == {"fans": [{"id": 0, "auto": True}]}


# ---- discovery: /probe fingerprint ---------------------------------


def _probe(model: str):
    return {"model": model, "hostname": "NMAxe_6b784", "ver": "v3.0.21"}


def test_identify_nmaxe_by_probe():
    with patch.object(discovery.NmaxeDriver, "fetch_probe", AsyncMock(return_value=_probe("NMAxe"))):
        info = asyncio.run(discovery._identify_nmaxe("10.0.0.9"))
    assert info is not None
    assert info["family"] == "nmaxe"
    assert info["model"] == "NMAxe"
    assert info["mac"] is None
    assert info["name"] == "NMAxe_6b784"
    assert info["port"] == discovery.PORT_BITAXE


def test_identify_nmqaxe_not_nerd():
    """Regression: "NMQAxe++" contains "qaxe" but must classify as nmaxe."""
    with patch.object(discovery.NmaxeDriver, "fetch_probe", AsyncMock(return_value=_probe("NMQAxe++"))):
        info = asyncio.run(discovery._identify_nmaxe("10.0.0.9"))
    assert info is not None
    assert info["family"] == "nmaxe"


def test_identify_nmaxe_absent_without_probe():
    """Stock AxeOS / NerdQAxe have no /probe → fetch_probe {} → no match."""
    with patch.object(discovery.NmaxeDriver, "fetch_probe", AsyncMock(return_value={})):
        info = asyncio.run(discovery._identify_nmaxe("10.0.0.9"))
    assert info is None


def test_identify_from_ports_runs_nmaxe_first():
    """_identify_from_ports must try NMAxe before the Bitaxe path so an
    NMAxe never reaches _identify_bitaxe (which would mislabel NMQAxe++)."""
    with patch.object(discovery.NmaxeDriver, "fetch_probe", AsyncMock(return_value=_probe("NMAxe"))), \
         patch.object(discovery, "_identify_bitaxe", AsyncMock(return_value={"family": "bitaxe"})):
        info = asyncio.run(discovery._identify_from_ports("10.0.0.9", [discovery.PORT_BITAXE]))
    assert info is not None
    assert info["family"] == "nmaxe"


# ---- live-share WS dialect -----------------------------------------


def test_ws_url_path_per_family():
    ls = LogStreamer()
    assert ls._ws_url("host", 80, "nmaxe") == "ws://host/ws"
    assert ls._ws_url("host", 80, "bitaxe") == "ws://host/api/ws"
    assert ls._ws_url("host", 8080, "nmaxe") == "ws://host:8080/ws"


def test_is_supported_includes_nmaxe():
    ls = LogStreamer()
    assert ls.is_supported("nmaxe") is True
    assert ls.is_supported("bitaxe") is True
    assert ls.is_supported("canaan") is False


def test_nmaxe_share_line_parsed():
    """The captured ₿-pipe share line yields one submitted ShareEvent with
    the exact share difficulty and the pool target."""
    ls = LogStreamer()
    stream = MinerStream(miner_id=1, host="10.0.0.9", port=80, family="nmaxe")
    line = "\x1b[32m₿ |32.00 |6.588K|2.049K|234.5394M|\x1b[0m"
    asyncio.run(ls._handle_nmaxe_line(stream, line))
    assert len(stream.buffer) == 1
    ev = stream.buffer[-1]
    assert round(ev.share_diff) == 6_588
    assert round(ev.pool_target) == 2_049
    assert ev.submitted is True
    assert stream.submitted_total == 1


def test_nmaxe_non_share_line_ignored():
    ls = LogStreamer()
    stream = MinerStream(miner_id=1, host="10.0.0.9", port=80, family="nmaxe")
    asyncio.run(ls._handle_nmaxe_line(stream, "\x1b[0mI (123) wifi: connected\x1b[0m"))
    assert len(stream.buffer) == 0


# ---- Our Control Tests ---------------------------------------------


def test_nmaxe_control_pause_resume():
    drv = NmaxeDriver("10.0.0.9")

    async def run():
        with patch("httpx.AsyncClient.patch") as mock_patch:
            mock_patch.return_value = Mock(status_code=200)

            res = await drv.pause()
            assert res is True
            mock_patch.assert_called_once_with(
                "http://10.0.0.9:80/api/mining/state",
                json={"paused": True}
            )

            mock_patch.reset_mock()
            res = await drv.resume()
            assert res is True
            mock_patch.assert_called_once_with(
                "http://10.0.0.9:80/api/mining/state",
                json={"paused": False}
            )

    asyncio.run(run())


def test_nmaxe_control_freq_volt_restart():
    drv = NmaxeDriver("10.0.0.9")

    async def run():
        with patch("httpx.AsyncClient.patch") as mock_patch, \
             patch("httpx.AsyncClient.post") as mock_post:
            mock_patch.return_value = Mock(status_code=200)
            mock_post.return_value = Mock(status_code=200)

            assert await drv.set_frequency(500) is True
            mock_patch.assert_called_once_with(
                "http://10.0.0.9:80/api/setting/mining",
                json={"asicFreqReq": 500}
            )

            mock_patch.reset_mock()
            assert await drv.set_voltage(1100) is True
            mock_patch.assert_called_once_with(
                "http://10.0.0.9:80/api/setting/mining",
                json={"asicVcoreReq": 1100}
            )

            assert await drv.restart() is True
            mock_post.assert_called_once_with("http://10.0.0.9:80/api/system/restart")

    asyncio.run(run())


def test_nmaxe_pool_config():
    drv = NmaxeDriver("10.0.0.9")

    mining_settings = {
        "stratum": {
            "primary": {"url": "stratum+tcp://solo.ckpool.org:3333", "user": "user1", "pwd": "p1"},
            "fallback": {"url": "stratum+tcp://pool.example.com:4444", "user": "user2", "pwd": "p2"}
        }
    }

    async def run():
        with patch("httpx.AsyncClient.get") as mock_get, \
             patch("httpx.AsyncClient.patch") as mock_patch, \
             patch("httpx.AsyncClient.post") as mock_post:

            mock_get.return_value = Mock(status_code=200, json=lambda: mining_settings)
            mock_patch.return_value = Mock(status_code=200)
            mock_post.return_value = Mock(status_code=200)

            # 1. Read config
            cfg = await drv.read_pool_config()
            assert cfg.url == "solo.ckpool.org"
            assert cfg.port == 3333
            assert cfg.user == "user1"
            assert cfg.password == "p1"
            assert cfg.fb_url == "pool.example.com"
            assert cfg.fb_port == 4444
            assert cfg.fb_user == "user2"
            assert cfg.fb_password == "p2"

            # 2. Set config
            new_cfg = PoolConfig(
                url="new.pool.com",
                port=5555,
                user="newuser",
                password="newpassword",
                fb_url="newfb.com",
                fb_port=6666,
                fb_user="newfbuser",
                fb_password="newfbpassword"
            )
            assert await drv.set_pool(new_cfg) is True

            mock_patch.assert_called_once_with(
                "http://10.0.0.9:80/api/setting/mining",
                json={
                    "stratum": {
                        "primary": {"url": "stratum+tcp://new.pool.com:5555", "user": "newuser", "pwd": "newpassword"},
                        "fallback": {"url": "stratum+tcp://newfb.com:6666", "user": "newfbuser", "pwd": "newfbpassword"}
                    }
                }
            )
            # Should restart automatically after set_pool
            mock_post.assert_called_once_with("http://10.0.0.9:80/api/system/restart")

    asyncio.run(run())


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)

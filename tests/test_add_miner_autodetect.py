# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the auto-detecting manual "Add miner" flow.

Covers backend/discovery.py:identify_host (single-host fingerprint) and
the POST /api/miners endpoint, which now probes the device and stores the
*detected* family instead of a user-declared one (the old behaviour that
mis-saved NerdOctaxe / NerdQAxe boards as plain ``bitaxe``).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from backend import discovery
from backend import main as main_mod
from backend.main import MinerCreate, api_create_miner


# ---- identify_host (single-host fingerprint) ----------------------

def test_identify_host_offline_returns_none():
    """No open port -> no identification (drives the Add-miner 400 path)."""
    with patch.object(discovery, "_open_ports", AsyncMock(return_value=[])):
        result = asyncio.run(discovery.identify_host("10.0.0.99", timeout=0.1))
    assert result is None


def test_identify_host_port80_uses_bitaxe_fingerprint():
    """Port 80 open -> AxeOS fingerprint, cgminer path not consulted."""
    detected = {"family": "nerdoctaxe", "host": "10.0.0.5", "port": 80}
    with patch.object(discovery, "_open_ports", AsyncMock(return_value=[discovery.PORT_BITAXE])), \
         patch.object(discovery, "_identify_bitaxe", AsyncMock(return_value=detected)) as ax, \
         patch.object(discovery, "_identify_cgminer", AsyncMock()) as cg:
        result = asyncio.run(discovery.identify_host("10.0.0.5", timeout=0.1))
    assert result == detected
    ax.assert_awaited_once_with("10.0.0.5")
    cg.assert_not_awaited()


def test_identify_host_port4028_uses_cgminer_fingerprint():
    """Only 4028 open -> cgminer fingerprint is used."""
    detected = {"family": "luxos", "host": "10.0.0.6", "port": 4028}
    with patch.object(discovery, "_open_ports", AsyncMock(return_value=[discovery.PORT_CGMINER])), \
         patch.object(discovery, "_identify_bitaxe", AsyncMock(return_value=None)), \
         patch.object(discovery, "_identify_cgminer", AsyncMock(return_value=detected)) as cg:
        result = asyncio.run(discovery.identify_host("10.0.0.6", timeout=0.1))
    assert result == detected
    cg.assert_awaited_once_with("10.0.0.6")


# ---- POST /api/miners (auto-detect + best-effort hard stop) -------

def test_create_miner_saves_detected_family_and_notes():
    """Endpoint persists the detected family/mac and layers user notes on top."""
    detected = {
        "family": "nerdoctaxe", "host": "10.0.0.5", "port": 80,
        "mac": "AA:BB:CC:DD:EE:FF", "model": "NerdOCTAXE-Gamma", "name": "octaxe.local",
    }
    upsert = AsyncMock(return_value=7)
    with patch.object(main_mod, "identify_host", AsyncMock(return_value=detected)), \
         patch.object(main_mod.db, "upsert_miner", upsert):
        out = asyncio.run(api_create_miner(MinerCreate(host="10.0.0.5", notes="garage")))
    assert out == {"id": 7}
    saved = upsert.await_args.args[0]
    assert saved["family"] == "nerdoctaxe"
    assert saved["mac"] == "AA:BB:CC:DD:EE:FF"
    assert saved["notes"] == "garage"


def test_create_miner_offline_raises_400_and_does_not_persist():
    """Unreachable host -> 400 with the address in the message, nothing saved."""
    upsert = AsyncMock()
    with patch.object(main_mod, "identify_host", AsyncMock(return_value=None)), \
         patch.object(main_mod.db, "upsert_miner", upsert):
        with pytest.raises(HTTPException) as ei:
            asyncio.run(api_create_miner(MinerCreate(host="10.0.0.99")))
    assert ei.value.status_code == 400
    assert "10.0.0.99" in str(ei.value.detail)
    upsert.assert_not_awaited()

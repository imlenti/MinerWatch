# SPDX-License-Identifier: AGPL-3.0-only
"""Auto-discovery of miners on the local network.

Scans the subnet (configurable CIDR) probing ports 80 and 4028.
For every IP that answers we try the supported drivers to determine
the family (Bitaxe/NerdQAxe via REST, Avalon/Braiins via cgminer-API).

How it works:
- Non-blocking TCP socket with a short timeout (~0.4s)
- For each candidate, poll with the matching driver
- On a valid reply the miner is UPSERTed in the DB using the MAC as
  the stable key.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Iterable

from .config import get_config
from . import db
from .miners import BitaxeDriver, BraiinsDriver, CanaanDriver, LuxosDriver
from .miners.cgminer_client import CgminerClient, CgminerError

log = logging.getLogger("minerwatch.discovery")

PORT_BITAXE = 80
PORT_CGMINER = 4028


async def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _local_cidr() -> str | None:
    """Guess the host's subnet, or ``None`` if it can't be determined.

    The trick: opening a UDP "connection" towards a public address
    (8.8.8.8:53) doesn't send anything on the wire, but it forces the
    kernel to pick the outbound interface — whose IP we then read with
    ``getsockname()``. From that we derive the local /24.

    Returns ``None`` instead of guessing on failure: a stale fallback
    like 192.168.1.0/24 is silently wrong on every other subnet
    (192.168.0.x, 10.x, 192.168.178.x …) and just makes auto-discovery
    look broken. The caller logs a clear message and points at the
    Settings page where the user can set ``scan_cidr`` manually.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 53))
            local_ip = s.getsockname()[0]
        net = ipaddress.IPv4Network(f"{local_ip}/24", strict=False)
        return str(net)
    except OSError:
        return None


def _enumerate_hosts(cidr: str) -> Iterable[str]:
    network = ipaddress.IPv4Network(cidr, strict=False)
    for host in network.hosts():
        yield str(host)


async def _identify_bitaxe(host: str) -> dict | None:
    """Identify an AxeOS-family host listening on port 80.

    Bitaxe and NerdQAxe/NerdOctaxe speak the same REST API on the same
    port. The authoritative way to tell them apart — and to obtain a
    human-readable model name — is ``deviceModel`` from
    ``/api/system/asic`` (e.g. "Gamma", "Supra", "SupraHex" for the
    classic Bitaxe line; "NerdQAxe++", "NerdOCTAXE-Gamma" for the
    multi-chip fork). See https://osmu.wiki/bitaxe/api.

    Classification, in order of trust:
      1. ``deviceModel`` contains "nerd"/"qaxe"/"octaxe" → ``nerdoctaxe``
      2. genuine fork-only telemetry, as a fallback for firmware that
         doesn't expose ``deviceModel``:
           - ``fanCount > 1``      → multi-fan board
           - ``currentA`` present  → NerdQAxe PSU sensor (the classic
             Bitaxe firmware reports ``current`` in mA, never ``currentA``)

    NOTE: ``fallbackStratumURL`` is deliberately NOT used as a signal.
    It is a standard field on *every* Bitaxe that has a fallback pool
    configured, so keying on it misclassified ordinary Bitaxes (Gamma,
    Supra…) as NerdOctaxe.
    """
    drv = BitaxeDriver(host=host, timeout=2)
    sample = await drv.poll()
    if not sample.online:
        return None

    raw = sample.raw or {}
    # Static ASIC/board identity (deviceModel, asicCount, …). Best-effort:
    # empty dict on older firmware that doesn't expose the endpoint.
    asic = await drv.fetch_asic_info()
    device_model = str(asic.get("deviceModel") or "").strip()

    fan_count = raw.get("fanCount")
    try:
        fan_count_int = int(fan_count) if fan_count is not None else 0
    except (TypeError, ValueError):
        fan_count_int = 0
    has_current_a = "currentA" in raw
    looks_nerd = any(
        tok in device_model.lower() for tok in ("nerd", "qaxe", "octaxe")
    )

    is_nerdoctaxe = looks_nerd or fan_count_int > 1 or has_current_a

    family = "nerdoctaxe" if is_nerdoctaxe else "bitaxe"
    default_model = "NerdOctaxe" if is_nerdoctaxe else "Bitaxe"
    # Prefer the firmware's own device model name; fall back to the ASIC
    # chip / board version (older firmware without /api/system/asic) and
    # finally to the family default.
    model = device_model or sample.model or default_model
    return {
        "family": family,
        "host": host,
        "port": PORT_BITAXE,
        "mac": sample.mac,
        "model": model,
        "name": sample.hostname or model or f"{default_model} {host}",
    }


async def _cgminer_fingerprint(host: str, timeout: float = 2.0) -> str | None:
    """Tell LuxOS / Braiins / Canaan apart on port 4028 by *raw key names*.

    The right discriminator is the **presence** of firmware-specific
    keys in the ``version`` response, not the value of any parsed
    field. Why this matters: every cgminer-compatible firmware fills
    the same generic keys (``API``, ``Miner``, sometimes ``Type``),
    so two drivers reading the same response can both come back with
    a non-empty ``firmware_version`` and ``model``. The original
    discovery checked the parsed strings for substrings like ``"lux"``,
    which produced false positives whenever a driver's *fallback
    default* (e.g. ``model = ... or "LuxOS"``) accidentally contained
    the marker. We hit exactly this on a Braiins-OS miner being
    classified as LuxOS because the LuxOS parser had defaulted model
    to "LuxOS" before discovery checked.

    Fingerprints, in priority order:
      1. ``LUXminer`` (or any key matching ``lux`` case-insensitively)
         → ``"luxos"``
      2. ``BOSminer`` / ``BOSminer+`` (or any key matching ``bos`` or
         ``braiins``) → ``"braiins"``
      3. Neither → assume ``"canaan"`` (catch-all for cgminer-on-:4028
         without a distinctive firmware marker). Avalon Nano 3s replies
         to ``version`` with ``CGMiner``/``Miner`` only, so this is the
         correct bucket for it too.

    Returns ``None`` if the host doesn't answer the ``version`` call at
    all (offline, wrong port, non-cgminer service).
    """
    cli = CgminerClient(host, PORT_CGMINER, timeout)
    try:
        version = await cli.call("version")
    except (CgminerError, OSError, asyncio.TimeoutError):
        return None

    # Normalize: VERSION may be a list-of-one (canonical) or a single
    # dict (some legacy builds). Reduce to the inner dict.
    v_block = version.get("VERSION")
    if isinstance(v_block, list) and v_block:
        v_dict = v_block[0] if isinstance(v_block[0], dict) else {}
    elif isinstance(v_block, dict):
        v_dict = v_block
    else:
        v_dict = {}
    if not v_dict:
        # cgminer answered but the shape is unrecognisable; still
        # better to bucket it as "canaan/fallback" than to drop it.
        return "canaan"

    keys_lower = {str(k).lower() for k in v_dict.keys()}
    if any("lux" in k for k in keys_lower):
        return "luxos"
    if any("bos" in k or "braiins" in k for k in keys_lower):
        return "braiins"
    return "canaan"


async def _identify_cgminer(host: str) -> dict | None:
    """Identify a host that has port 4028 open.

    Two-step: first a lightweight ``version`` call to fingerprint the
    firmware family by raw key names (see ``_cgminer_fingerprint``),
    then a full poll with the matching driver to harvest metadata
    (MAC, hashboard model, hostname). This way the LuxOS / Braiins /
    Canaan branches never see each other's responses.
    """
    family = await _cgminer_fingerprint(host)
    if family is None:
        return None

    drv: LuxosDriver | BraiinsDriver | CanaanDriver
    if family == "luxos":
        drv = LuxosDriver(host=host, timeout=2)
    elif family == "braiins":
        drv = BraiinsDriver(host=host, timeout=2)
    else:  # canaan / fallback
        drv = CanaanDriver(host=host, timeout=2)

    sample = await drv.poll()
    if not sample.online:
        # We fingerprinted successfully but the full poll bombed —
        # unusual (probably a transient network blip). Still register
        # the host so the user sees something rather than nothing,
        # using the fingerprint family.
        return {
            "family": family,
            "host": host,
            "port": PORT_CGMINER,
            "mac": None,
            "model": _default_model(family),
            "name": _default_name(family, host),
        }

    return {
        "family": family,
        "host": host,
        "port": PORT_CGMINER,
        "mac": sample.mac,
        "model": sample.model or _default_model(family),
        "name": sample.model or _default_name(family, host),
    }


def _default_model(family: str) -> str:
    return {
        "luxos": "LuxOS",
        "braiins": "BMM101",
        "canaan": "Avalon",
    }.get(family, family.title())


def _default_name(family: str, host: str) -> str:
    """Friendly fallback name when the firmware reports no model.

    The Braiins BMM (Mini Miner) firmware doesn't expose a model in its
    cgminer ``version`` payload, so without this we'd fall back to
    "Braiins <ip>". The BMM101 is the only Braiins device that omits the
    model, so we present it with its full product label. Other families
    keep the host suffix to disambiguate identical models on the network.
    """
    if family == "braiins":
        return "Braiins BMM101"
    return f"{_default_model(family)} {host}"


async def _identify_from_ports(host: str, ports: list[int]) -> dict | None:
    """Run the matching driver fingerprint for a host with known open ports.

    Tries the AxeOS REST identity first (port 80), then the cgminer
    family (port 4028). Returns the discovery dict
    (``{family, host, port, mac, model, name}``) or ``None`` when no
    driver could identify the host. Shared by both the subnet
    :func:`scan_network` and the single-host :func:`identify_host`.
    """
    info = None
    if PORT_BITAXE in ports:
        info = await _identify_bitaxe(host)
    if not info and PORT_CGMINER in ports:
        info = await _identify_cgminer(host)
    return info


async def _open_ports(host: str, timeout: float) -> list[int]:
    """Probe ports 80 and 4028 in parallel; return the ones that answer."""
    results = await asyncio.gather(
        _port_open(host, PORT_BITAXE, timeout),
        _port_open(host, PORT_CGMINER, timeout),
    )
    return [p for p, is_open in zip([PORT_BITAXE, PORT_CGMINER], results) if is_open]


async def identify_host(host: str, timeout: float | None = None) -> dict | None:
    """Fingerprint a single host the same way auto-discovery does.

    This is the single-host counterpart of :func:`scan_network`: it
    probes the two well-known miner ports (80 for AxeOS, 4028 for the
    cgminer family) and, on a reply, runs the matching driver to derive
    the family, model, MAC and a friendly name -- exactly the metadata
    the subnet scan collects. Used by the manual "Add miner" flow so a
    hand-entered IP/hostname gets the *detected* family instead of a
    user-declared one (which is how NerdOctaxe / NerdQAxe boards used to
    get mis-saved as plain ``bitaxe``).

    ``host`` may be an IP or a hostname. ``timeout`` defaults to the
    configured ``network.scan_timeout``. Returns the discovery dict
    (``{family, host, port, mac, model, name}``) or ``None`` when the
    host doesn't answer on either port / can't be identified.
    """
    if timeout is None:
        timeout = get_config().network.scan_timeout
    ports = await _open_ports(host, timeout)
    if not ports:
        return None
    return await _identify_from_ports(host, ports)


async def scan_network(cidr: str | None = None) -> list[dict]:
    cfg = get_config()
    if not cidr:
        cidr = cfg.network.scan_cidr
    if cidr in (None, "", "auto"):
        cidr = _local_cidr()
        if cidr is None:
            log.warning(
                "Discovery: could not auto-detect the host's subnet "
                "(no network interface or no default route). "
                "Set network.scan_cidr from the Settings page (e.g. "
                "192.168.0.0/24 or 10.0.0.0/24) and retry."
            )
            return []
    log.info("Discovery: scanning %s", cidr)

    hosts = list(_enumerate_hosts(cidr))
    timeout = cfg.network.scan_timeout

    # Step 1: find which hosts have at least one open port
    sem = asyncio.Semaphore(64)

    async def probe(host: str) -> tuple[str, list[int]]:
        async with sem:
            open_ports = await _open_ports(host, timeout)
        return host, open_ports

    probes = await asyncio.gather(*(probe(h) for h in hosts))
    candidates = [(h, ports) for h, ports in probes if ports]
    log.info("Discovery: %d candidate host(s)", len(candidates))

    # Step 2: identify the family with the matching driver
    found: list[dict] = []
    for host, ports in candidates:
        info = await _identify_from_ports(host, ports)
        if info:
            found.append(info)

    log.info("Discovery: identified %d miner(s)", len(found))
    return found


async def discover_and_register() -> list[dict]:
    """Run the scan and UPSERT the miners found into the DB."""
    found = await scan_network()
    for info in found:
        await db.upsert_miner(info)
    return found

# SPDX-License-Identifier: AGPL-3.0-only
"""Common interface shared by all miner drivers.

A driver is a stateless object that knows how to talk to a single
miner via IP. It exposes async methods ``poll()`` (reads current
metrics) and optionally ``set_fan_speed(...)``, ``set_frequency(...)``,
etc.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BoardSnapshot:
    """Per-hashboard snapshot.

    Populated by drivers (currently only LuxOS) that can report the
    state of each hashboard separately. Multi-board firmwares like
    Antminer/Whatsminer expose freq, voltage, temperatures and chip
    health on a per-board basis; aggregating those into a single
    miner-level reading loses the per-board topology that operators
    rely on to spot a faulty board.

    All fields are optional. Each driver fills what it can read; the
    frontend renders a tile per board and skips the rows that are
    None. Temperatures are kept under ``temps_extra`` rather than
    spelled out one field per sensor because LuxOS surfaces them by
    *position* (BottomLeft/BottomRight/TopLeft/TopRight) with the
    human-readable label living in a sibling ``METADATA`` block —
    keeping the original key + the label preserves both pieces of
    information for the frontend.
    """

    id: int
    status: str | None = None              # e.g. "Alive" / "Dead"
    enabled: bool | None = None            # cgminer "Enabled" field
    connector: str | None = None           # physical board connector (e.g. "J6")

    # Performance
    frequency_mhz: float | None = None
    voltage_v: float | None = None         # board input voltage (not core mV)
    hashrate_ths: float | None = None      # rolling avg (typically MHS 1m / 1e6)
    hashrate_5s_ths: float | None = None
    nominal_ths: float | None = None       # expected/nominal hashrate per board
    # Hardware error rate (%) for this board. LuxOS reports
    # ``Device Hardware%`` as a hard-coded 0, so we compute it ourselves
    # from the board's ``Hardware Errors`` and ``Diff1 Work`` counters
    # (classic cgminer formula: HW / (HW + Diff1Work) * 100). None when
    # the firmware didn't expose the underlying counters.
    hw_error_rate: float | None = None

    # Thermal — chip is the worst case across chips, others are per-sensor.
    # ``temps_extra`` carries every named sensor LuxOS exposes, keyed by
    # its raw position name (BottomLeft, …). ``temps_labels`` mirrors
    # METADATA so the frontend can display "Board Outlet (top)" etc.
    temp_chip_c: float | None = None
    temps_extra: dict[str, float] = field(default_factory=dict)
    temps_labels: dict[str, str] = field(default_factory=dict)

    # Chip health (from LuxOS healthchipget)
    chips_total: int | None = None
    chips_healthy: int | None = None
    chips_unhealthy: int | None = None
    chips_unknown: int | None = None
    # Per-chip records, in board-iteration order. Schema mirrors LuxOS:
    #   {"chip": int, "row": int, "column": int, "domain": int,
    #    "healthy": "Y"|"N"|"Unknown", "frequency": float|None,
    #    "ghs_1m": float|None, "ghs_5m": float|None,
    #    "score": float|None, "chip_temp_c": float|None,
    #    "hash_count": int|None, "hash_expected": int|None,
    #    "is_checking": bool|None}
    # Kept as plain dicts (not a nested dataclass) because the schema
    # is shaped by what the firmware returns; making it a class adds
    # noise without making it more correct.
    chips: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FanSnapshot:
    """Per-fan snapshot.

    Drivers that talk to multi-fan firmware (LuxOS, BOSminer) populate
    one of these per physical fan. The legacy ``fan_rpm`` / ``fan_pct``
    / ``fans_extra`` fields on :class:`MinerSample` are kept for
    backward compatibility with the time-series DB and with other
    drivers that report a single fan.
    """

    id: int
    rpm: int | None = None
    speed_pct: float | None = None
    connector: str | None = None  # e.g. "J12 | J14" — LuxOS only


@dataclass
class PoolSnapshot:
    """Per-pool snapshot — one entry per pool slot configured on a miner.

    Mirrors what the cgminer-family ``pools`` command returns 1:1 where
    possible, and is synthesised from firmware fields on AxeOS (Bitaxe /
    NerdOctaxe). The frontend reads ``MinerSample.pools`` to render the
    fleet-wide Pools page.

    Field availability by driver:

      * cgminer-family (Braiins/LuxOS/Canaan): url, user, status,
        priority, accepted, rejected, **stale**, last_share_ts, active.
        Some builds also expose ``Diff1 Shares`` and per-pool reject %.
      * Bitaxe: url, user, accepted, rejected. ``status`` is inferred
        from miner liveness (no explicit Alive/Dead per-pool flag in
        AxeOS). ``stale`` is not exposed by the firmware — stays None.
      * NerdOctaxe: same as Bitaxe, with a second entry for the
        fallback slot. ``active`` is True for whichever the firmware
        reports via ``stratum.activePoolMode`` / ``usingFallback``.

    ``status`` values: ``"alive"`` / ``"dead"`` / ``"disabled"`` /
    ``None`` (unknown — left to the frontend to interpret).
    """

    url: str | None = None
    user: str | None = None
    status: str | None = None         # "alive" | "dead" | "disabled" | None
    priority: int | None = None       # cgminer "Priority"; lower = preferred
    accepted: int | None = None
    rejected: int | None = None
    stale: int | None = None          # not surfaced by AxeOS firmware
    last_share_ts: int | None = None  # epoch seconds; cgminer "Last Share Time"
    active: bool | None = None        # this is the slot the miner is mining on
    slot: str | None = None           # "primary" / "fallback" hint for AxeOS
    # Round-trip latency to the pool, in milliseconds, as measured by
    # the *miner itself* (not a server-side probe). Field availability:
    #   * Bitaxe (AxeOS):       ``responseTime`` — miner-level, attributed
    #                           to the single pool slot.
    #   * NerdQAxe/NerdOctaxe:  ``stratum.pools[i].pingRtt`` — per-pool.
    #   * Avalon/Canaan:        ``PING[..]`` inside the MM ID0 string —
    #                           miner-level, attributed to the active pool.
    #   * Braiins / LuxOS:      not exposed by cgminer ``pools`` — stays None.
    ping_ms: float | None = None
    # Packet-loss % for the ping, when the firmware reports it
    # (NerdQAxe ``pingLoss``). None everywhere else.
    ping_loss: float | None = None


@dataclass
class MinerSample:
    """Snapshot of a miner's current metrics.

    All fields are optional: each driver fills the ones it can read
    from its firmware. Unavailable fields stay ``None`` and appear as
    "—" in the frontend.
    """

    family: str
    host: str
    online: bool = True
    error: str | None = None

    # Identity
    mac: str | None = None
    model: str | None = None
    # ASIC chip model (e.g. "BM1370"). Not reported by any LuxOS API
    # field, so drivers derive it from the miner model via a lookup
    # table. Stays None when the model is unknown/unmapped. Mirrors what
    # AxeOS-family firmware exposes directly as ``ASICModel``.
    chip_model: str | None = None
    hostname: str | None = None
    firmware_version: str | None = None

    # Performance
    hashrate_ths: float | None = None  # TH/s
    power_w: float | None = None
    efficiency_w_per_ths: float | None = None

    # Thermal
    # ``temp_chip_c`` is the *hottest* chip sensor — every multi-sensor
    # driver (LuxOS/Braiins/Canaan, and Bitaxe on multi-ASIC boards)
    # collapses its sensors with max() here, because this is the field the
    # overheat alert and the auto-fan PID regulate on. It must track the
    # hottest cluster, not an arbitrary single sensor.
    temp_chip_c: float | None = None
    # Second chip-temperature sensor, exposed by multi-ASIC AxeOS boards
    # such as the Bitaxe SupraHex (6× BM1368), which report two on-board
    # sensors as ``temp`` / ``temp2``. Mirrors the ``fan_rpm_2`` precedent:
    # a dedicated field for the second physical sensor so the frontend can
    # show both readings. Stays None on single-sensor devices and on every
    # driver that doesn't populate it.
    temp_chip_2_c: float | None = None
    temp_vr_c: float | None = None
    temp_outlet_c: float | None = None  # Avalon/Canaan: OTemp
    temp_inlet_c: float | None = None   # Avalon/Canaan: ITemp (often unavailable)
    temp_avg_c: float | None = None     # chip average (TAvg)
    fan_rpm: int | None = None
    fan_pct: float | None = None
    fans_extra: dict[str, int] = field(default_factory=dict)
    # Structured per-fan list — populated by drivers that have richer
    # per-fan metadata than the plain {id: rpm} map in ``fans_extra``.
    # Today only LuxOS fills this (RPM + Speed% + physical connector
    # label like "J12 | J14"). The legacy ``fan_rpm`` / ``fan_pct`` /
    # ``fans_extra`` fields are still populated for time-series logging
    # and for drivers that only know one fan; ``fans`` is additive and
    # only consumed by the frontend's N-ary fan renderer.
    fans: list[FanSnapshot] = field(default_factory=list)
    # NerdOctaxe has a second physical fan (the firmware exposes
    # `fanrpm2`/`fanspeed2`). The Bitaxe-family base only carried a
    # single fan; rather than abusing `fans_extra` we expose dedicated
    # second-fan fields so the frontend can render them with the same
    # styling as the primary fan. They stay None for any driver that
    # doesn't populate them.
    fan_rpm_2: int | None = None
    fan_pct_2: float | None = None

    # ASIC
    frequency_mhz: float | None = None
    voltage_mv: float | None = None
    asic_count: int | None = None
    # Multi-hashboard miners (S19/S21 + LuxOS, BMM, …) report one
    # entry per physical board. ``board_count`` is the length of that
    # list, ``chip_count`` is the total ASIC chip count across all
    # boards. ``asic_count`` historically conflated the two: drivers
    # were storing the *board* count there because cgminer's ``devs``
    # returns one row per board. We keep ``asic_count`` populated for
    # backward compatibility (older callers expect it) but new code
    # should prefer ``board_count`` / ``chip_count``.
    board_count: int | None = None
    chip_count: int | None = None
    boards: list[BoardSnapshot] = field(default_factory=list)

    # PSU draw in Amps. Bitaxe doesn't surface this directly, but
    # NerdOctaxe firmware does (`currentA` in /api/system/info).
    current_a: float | None = None

    # Aggregate hardware-error counter exposed by the NerdQAxePlus
    # firmware as `duplicateHWNonces` — count of nonces the ASIC
    # returned that failed validation. There is *no* per-chip error
    # rate in the firmware, so this is the most "chip-error-like"
    # signal available. The frontend can also derive a rejection rate
    # from accepted/rejected; we expose the raw count here.
    hw_errors: int | None = None
    # Raw "total" field from the AxeOS hashrateMonitor (`total` per ASIC,
    # summed). Despite the name this is NOT a work counter — on a real device
    # it equals the ASIC hashrate (GH/s) — so it is NOT a usable denominator
    # for an error %. Kept as telemetry only; the Guardian governs on the
    # reject rate instead. See backend/miners/bitaxe.py:_hw_total_from_monitor.
    hw_total: int | None = None
    # Fleet-wide hardware error rate (%), aggregated across all boards.
    # Computed (not read) on LuxOS because its ``Device Hardware%`` field
    # is hard-coded to 0 — see BoardSnapshot.hw_error_rate. 2-decimal
    # presentation is left to the frontend. None when uncomputable.
    hw_error_rate: float | None = None

    # Mining
    uptime_s: int | None = None
    accepted: int | None = None
    rejected: int | None = None
    best_difficulty: float | None = None
    # Optional all-time best, when the firmware persists it across
    # reboots (Bitaxe NVS exposes `bestDiff`). Drivers that don't have
    # persistent storage (cgminer/BOSminer/Avalon `Best Share`) leave
    # this None — MinerWatch then derives all-time from its own DB.
    best_difficulty_alltime: float | None = None
    # Current Bitcoin network difficulty as seen by the miner via
    # stratum. AxeOS exposes this in ``networkDifficulty``. When a share
    # difficulty meets or exceeds this value, the miner has effectively
    # found a block. Drivers that don't surface it leave None; MinerWatch
    # falls back to a periodic fetch from a public block-explorer API.
    network_difficulty: float | None = None
    pool_url: str | None = None
    worker: str | None = None

    # Dual-pool (NerdOctaxe / NerdQAxe Plus firmware). The primary pool
    # lives in `pool_url`/`worker` above; these mirror the firmware's
    # fallback-pool config so the frontend can show "Pool 1 / Pool 2".
    # `pool_active` is "primary" | "fallback" when known (derived from
    # `stratum.activePoolMode` or `stratum.usingFallback`), or None.
    pool_url_fallback: str | None = None
    worker_fallback: str | None = None
    pool_active: str | None = None

    # Structured per-pool list — one entry per pool slot configured on
    # the miner, including the fallback slot(s). This is what feeds the
    # fleet-wide /pools page. The legacy ``pool_url`` / ``worker`` /
    # ``pool_url_fallback`` / ``worker_fallback`` / ``pool_active``
    # scalars above stay populated for backward compatibility (the
    # dashboard cards, the DB, and the alerts pipeline all still read
    # them). ``pools`` is additive: drivers that haven't been migrated
    # yet leave it empty and the frontend falls back to synthesising a
    # single entry from the legacy fields.
    pools: list[PoolSnapshot] = field(default_factory=list)

    # Original payload for debugging / for extracting non-standard fields
    raw: dict[str, Any] | None = None

    def to_db_sample(self) -> dict[str, Any]:
        """Shape suitable for :func:`db.insert_metric`."""
        return {
            "hashrate_ths": self.hashrate_ths,
            "power_w": self.power_w,
            "temp_chip_c": self.temp_chip_c,
            "temp_vr_c": self.temp_vr_c,
            "fan_rpm": self.fan_rpm,
            "fan_pct": self.fan_pct,
            "frequency_mhz": self.frequency_mhz,
            "voltage_mv": self.voltage_mv,
            "uptime_s": self.uptime_s,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "best_difficulty": self.best_difficulty,
            "pool_url": self.pool_url,
            "worker": self.worker,
            "raw": self.raw,
        }


# Suffix → multiplier table for SI-formatted difficulty / share strings.
# AxeOS prints values like "4.29G", "2.15M", "512k"; cgminer/BOSminer
# usually returns raw numbers but some BOS+ builds also use the SI form.
# All keys are lowercase; the parser is case-insensitive.
_SI_SUFFIXES = {
    "": 1.0,
    "k": 1e3,
    "m": 1e6,
    "g": 1e9,
    "t": 1e12,
    "p": 1e15,
    "e": 1e18,
    "z": 1e21,
    "y": 1e24,
}


def parse_si_difficulty(value: Any) -> float | None:
    """Parse a difficulty value as either a number or an SI-suffixed string.

    Returns ``None`` for missing / unparseable input. Examples:

        parse_si_difficulty("4.29G")     -> 4.29e9
        parse_si_difficulty("2.15 M")    -> 2.15e6
        parse_si_difficulty(49224525)    -> 49224525.0
        parse_si_difficulty("0")         -> 0.0
        parse_si_difficulty(None)        -> None
        parse_si_difficulty("")          -> None
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    # Last char might be a SI suffix. Tolerate a space between number
    # and suffix and an optional trailing letter (e.g. "B" for "Bytes"
    # in some Antminer builds — irrelevant here but harmless).
    suffix_char = s[-1].lower()
    if suffix_char.isalpha() and suffix_char in _SI_SUFFIXES:
        num_part = s[:-1].strip()
        mult = _SI_SUFFIXES[suffix_char]
    else:
        num_part = s
        mult = 1.0
    try:
        return float(num_part) * mult
    except (TypeError, ValueError):
        return None


def _opt_int_basic(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def parse_cgminer_pool_entry(entry: dict[str, Any]) -> PoolSnapshot:
    """Convert one row from the cgminer ``pools`` reply into a :class:`PoolSnapshot`.

    Field names are stable across cgminer/BOSminer/LuxOS/Avalon but
    *casing and presence* differ between builds. We accept the common
    aliases and tolerate missing keys (any unknown field becomes None).

    A few subtleties handled here:

      * ``Status`` arrives as ``"Alive"`` / ``"Dead"`` / ``"Disabled"``;
        we normalise to lowercase for the wire to keep the frontend
        comparison case-insensitive.
      * ``Stratum Active`` and ``connected`` are different ways
        different firmwares mark "this is the pool currently mining".
        We surface both via ``active``.
      * ``Last Share Time`` is epoch seconds on cgminer and some builds
        return ``"0"`` to mean "never" — preserve that semantic by
        leaving ``last_share_ts=None`` for zero.
    """
    if not isinstance(entry, dict):
        return PoolSnapshot()

    url = entry.get("Stratum URL") or entry.get("URL") or entry.get("url")
    user = entry.get("User") or entry.get("user")
    priority = _opt_int_basic(entry.get("Priority"))

    status_raw = entry.get("Status")
    status: str | None = None
    if isinstance(status_raw, str) and status_raw.strip():
        status = status_raw.strip().lower()

    accepted = _opt_int_basic(entry.get("Accepted"))
    rejected = _opt_int_basic(entry.get("Rejected"))
    # cgminer/BOSminer field for stale-rejected shares. Some Avalon
    # builds spell it "Stale" with title-case; LuxOS keeps the same.
    stale = _opt_int_basic(entry.get("Stale"))

    # Last Share Time is the epoch second of the last accepted share
    # for *this pool slot*. Cgminer emits 0 when there hasn't been one
    # yet; treat that as "no data" so the UI shows "—" not "Dec 1969".
    last_share = _opt_int_basic(
        entry.get("Last Share Time")
        or entry.get("LastShareTime")
    )
    if last_share == 0:
        last_share = None

    # "This pool is currently mining". Two firmware signals:
    #   * "Stratum Active" → string "true"/"false" (cgminer / LuxOS / Avalon)
    #   * "connected"       → bool (some BOS+ builds and AxeOS dual-pool)
    active: bool | None = None
    sa = entry.get("Stratum Active")
    if isinstance(sa, bool):
        active = sa
    elif isinstance(sa, str) and sa.strip():
        active = sa.strip().lower() == "true"
    elif isinstance(entry.get("connected"), bool):
        active = entry["connected"]

    return PoolSnapshot(
        url=url if isinstance(url, str) and url else None,
        user=user if isinstance(user, str) and user else None,
        status=status,
        priority=priority,
        accepted=accepted,
        rejected=rejected,
        stale=stale,
        last_share_ts=last_share,
        active=active,
    )


def assign_cgminer_pool_slots(pools: list[PoolSnapshot]) -> None:
    """Tag cgminer-family pools with ``"primary"`` / ``"fallback"`` slots.

    The cgminer ``pools`` reply has no explicit primary/fallback flag the
    way AxeOS does — it only carries ``Priority`` (lower = more preferred,
    primary = the lowest). :func:`parse_cgminer_pool_entry` therefore can't
    fill ``slot`` on its own (it sees one entry at a time), so it leaves it
    ``None``. That left every Braiins/Canaan pool untagged, so the
    fleet-wide Pools page — which keys its "Fallback" filter and badge off
    ``slot == "fallback"`` — never recognised their backup pools (whereas
    the Bitaxe/NerdOctaxe drivers set ``slot`` directly and worked fine).

    Callers pass the list **already sorted by priority** (both cgminer
    drivers sort before calling this). We tag the most-preferred slot
    ``"primary"`` and every remaining slot ``"fallback"``. This is
    deliberately position-based rather than ``priority == 0`` so it stays
    correct on firmwares whose priorities aren't zero-based.

    Mutates ``pools`` in place. ``slot`` is independent of ``active``: a
    miner that has failed over to its backup still has that pool tagged
    ``"fallback"`` (the frontend shows it under both Active and Fallback).
    No-op for an empty list.
    """
    for i, pool in enumerate(pools):
        pool.slot = "primary" if i == 0 else "fallback"


@dataclass
class PoolConfig:
    """A miner's pool configuration, captured for snapshot/restore.

    Models the primary stratum slot plus the optional fallback slot that
    AxeOS / ESP-Miner exposes on all Bitaxe-class boards. ``url`` is the
    bare host without the port (e.g. ``solo.ckpool.org``); ``port`` is
    kept separate so drivers can map cleanly onto firmware fields
    (AxeOS ``stratumURL`` / ``stratumPort``).

    Used by the "Donate hashrate" feature: before repointing a miner we
    ``read_pool_config()`` and persist this as JSON, then restore it on
    revert. Keeping the fallback slot means revert is faithful even when
    the user had a custom backup pool.
    """

    url: str | None = None
    port: int | None = None
    user: str | None = None          # worker / stratum username
    password: str | None = None
    fb_url: str | None = None        # fallback slot
    fb_port: int | None = None
    fb_user: str | None = None
    fb_password: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "url": self.url,
                "port": self.port,
                "user": self.user,
                "password": self.password,
                "fb_url": self.fb_url,
                "fb_port": self.fb_port,
                "fb_user": self.fb_user,
                "fb_password": self.fb_password,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "PoolConfig":
        data = json.loads(raw) if raw else {}
        return cls(
            url=data.get("url"),
            port=data.get("port"),
            user=data.get("user"),
            password=data.get("password"),
            fb_url=data.get("fb_url"),
            fb_port=data.get("fb_port"),
            fb_user=data.get("fb_user"),
            fb_password=data.get("fb_password"),
        )


class MinerDriver:
    """Base class. Subclasses must override ``DEFAULT_PORT`` and ``poll``."""

    family: str = "generic"
    DEFAULT_PORT: int = 80
    can_set_fan: bool = False
    can_set_frequency: bool = False
    can_set_voltage: bool = False
    can_restart: bool = False
    # Whether this family supports repointing its pool via the API. Gates
    # the "Donate hashrate" feature: families with can_set_pool=False show
    # the donate action disabled. See docs/donate-hashrate-design.md.
    can_set_pool: bool = False

    def __init__(self, host: str, port: int | None = None, timeout: int = 4) -> None:
        self.host = host
        self.port = port or self.DEFAULT_PORT
        self.timeout = timeout

    # ---- API implemented by subclasses ----

    async def poll(self) -> MinerSample:
        raise NotImplementedError

    async def set_fan_speed(self, percent: int) -> bool:  # noqa: D401
        """Set fan speed as a percentage (0-100)."""
        raise NotImplementedError

    async def set_frequency(self, mhz: int) -> bool:
        raise NotImplementedError

    async def set_voltage(self, millivolts: int) -> bool:
        raise NotImplementedError

    async def restart(self) -> bool:
        raise NotImplementedError

    async def read_pool_config(self) -> "PoolConfig | None":
        """Capture the current pool config so it can be restored later.

        Returns ``None`` (or raises) if the driver can't read it. Drivers
        that set ``can_set_pool = True`` must implement this so the
        donation flow can snapshot before switching.
        """
        raise NotImplementedError

    async def set_pool(self, config: "PoolConfig") -> bool:
        """Repoint the miner at ``config``. Returns True if the command
        was accepted by the miner.

        NOTE: acceptance != applied. Some firmwares (AxeOS) only pick up
        a new stratum after a restart, and confirmation that the miner is
        actually mining the new pool comes from the poller, not from this
        return value.
        """
        raise NotImplementedError

    async def active_slot(self) -> "str | None":
        """Return which configured pool slot the miner is *currently*
        mining on: ``"primary"``, ``"fallback"``, or ``None`` when the
        driver can't tell (the default).

        The donation flow uses this to repoint whichever slot is live. A
        miner that has failed over to its fallback keeps mining the
        fallback after a restart (AxeOS persists ``isUsingFallbackStratum``
        in NVS), so rewriting only the primary slot would never take
        effect. Drivers that don't override this behave as before — the
        caller treats ``None`` as "primary".
        """
        return None

    # ---- Helpers ----

    def __repr__(self) -> str:  # pragma: no cover
        return f"{self.__class__.__name__}({self.host}:{self.port})"

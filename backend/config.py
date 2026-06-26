# SPDX-License-Identifier: AGPL-3.0-only
"""MinerWatch configuration loading and management.

Precedence order:
  1. Runtime overrides stored in the DB (`settings` table)
  2. config.yaml (if present in the repo root)
  3. config.example.yaml (default)
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
# FRONTEND_DIR points at the React bundle Vite emits. The legacy
# vanilla frontend at ./frontend/ was retired in P1 session 5; if you
# need to recover one of its files, check git history before the
# session-5 commit.
FRONTEND_DIR = ROOT_DIR / "frontend-react" / "dist"


@dataclass
class ServerCfg:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class NetworkCfg:
    scan_cidr: str = "auto"
    scan_timeout: float = 0.4


@dataclass
class PollingCfg:
    interval_seconds: int = 5
    request_timeout: int = 4
    # Time constant (tau) for the EMA used to smooth hashrate before
    # exposing/persisting it. 60s = good trade-off between responsiveness
    # and stability on stochastic miners (Poisson). 0 = disable smoothing (raw).
    hashrate_smoothing_seconds: int = 60


@dataclass
class StorageCfg:
    # Tiered retention: each tier is a separate aggregation level kept
    # for a different amount of time. The poller aggregates raw 5s
    # samples into 1-minute and 1-hour rollup tables, then prunes each
    # table according to its own retention.
    #
    # `retention_days` is kept for backward compatibility — older
    # config files set only this knob. When it's the only value
    # provided, we map it onto `retention_1m_days` (the "main" tier
    # users mostly look at).
    retention_raw_hours: int = 48
    retention_1m_days: int = 30
    retention_1h_days: int = 730
    retention_days: int = 30  # deprecated alias for retention_1m_days


@dataclass
class AlertsCfg:
    temp_chip_threshold: float = 75.0
    temp_vr_threshold: float = 90.0
    offline_threshold_seconds: int = 60
    # If a threshold is still exceeded N seconds after the first alert,
    # we emit another one (and another push). Default: 10 min.
    repeat_seconds: int = 600
    # Global kill-switch for ALL notifications (every channel). When False,
    # the notification dispatcher returns immediately. Subscriptions and
    # tokens are kept untouched, so re-enabling restores delivery without
    # any further action from the user. It's a clean "do not disturb".
    notifications_enabled: bool = True
    # Per-channel toggles. Browser push works only in secure contexts
    # (https or localhost) — useless on a LAN IP from an iPhone. The
    # Telegram channel covers exactly that gap: it's an outbound HTTP
    # POST from the server, so it works regardless of how the user
    # reaches the dashboard.
    push_enabled: bool = True
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    # String (not int) so it transparently supports group chats whose
    # IDs are negative numbers like "-1001234567890".
    telegram_chat_id: str = ""
    # Appends a short "star us on GitHub" / "donations welcome" footer to
    # *milestone* Telegram messages only (new all-time best share, block
    # found) — never to operational alerts like temperature or offline.
    # The best-share footer is rate-limited server-side; the block-found
    # one always ships (it's a once-in-a-lifetime event). Telegram-only:
    # browser push bodies stay clean (see alerts.telegram_extra).
    telegram_star_footer: bool = True
    # ---- Watched Bitcoin addresses (see backend/wallet_watch.py). ----
    # Feature switch for the whole watcher loop. With the default empty
    # address list this is a no-op, so True is a safe default: adding an
    # address in Settings is the only step needed to activate it.
    wallet_watch_enabled: bool = True
    # JSON string (NOT a YAML list) so it round-trips through the
    # settings DB and apply_overrides(), which only handle scalars.
    # Shape: [{"address": "bc1…", "label": "Donations"}, …]
    wallet_watch_addresses: str = "[]"
    # Confirmed incoming amounts at or below this many sats are still
    # notified, but flagged as a potential dust attack instead of a
    # regular payment. 546 sats = the classic P2PKH dust limit.
    wallet_watch_dust_sats: int = 546


@dataclass
class AuthCfg:
    enabled: bool = False
    password: str = ""


@dataclass
class GuardianCfg:
    """Configuration for the Guardian — a runtime frequency governor.

    The Guardian is a continuous, slow control loop (a twin of the
    server-side auto-fan PID in ``auto_control.py``, but acting on
    *frequency* instead of the fan). Per enabled miner it watches the VR
    temperature and the rejected-share rate and nudges the ASIC frequency
    to keep both inside safe bounds, recovering frequency when conditions
    cool down. It never goes above a per-miner ceiling (``max`` frequency,
    which defaults to the miner's current frequency). See
    ``docs/guardian-design.md`` for the full design, including the v2 plan
    that adds a voltage lever.

    Control loop (v1, frequency-only), evaluated once per ``interval_seconds``:
      - VR temp   > ``vr_high_c``      → frequency − ``step_down_vr_mhz``
      - reject %  > ``reject_pct_max`` → frequency − ``step_down_err_mhz``
      - VR temp   < ``vr_low_c``       → frequency + ``step_up_mhz`` (≤ ceiling)
      - otherwise (deadband)           → hold
    Downward (safety) actions take priority over the upward recovery, and
    every result is clamped to [floor, ceiling]. Because AxeOS applies
    frequency changes live (no reboot needed), the limiting factor is the
    VR thermal settle time, which is why the cadence — not downtime — is
    the main safety knob: keep ``interval_seconds`` ≥ the VR response time.

    These are GLOBAL defaults. The per-miner opt-in (``guardian_enabled``),
    the ``max`` frequency ceiling (``guardian_max_freq_mhz``) and an
    optional floor override (``guardian_freq_floor_mhz``) live on the
    ``miners`` row so each device is governed independently.

    ``enabled`` is the global feature flag. Flip it to False to disable the
    whole governor (the loop idles, the API endpoints report disabled and
    the Advanced UI hides the controls) without removing any code.
    """

    enabled: bool = True

    # How often the governor re-evaluates each miner. 5 min mirrors the
    # cadence that works well in practice: long enough for the VR to settle
    # after a live frequency change before the next decision is taken.
    interval_seconds: int = 300

    # Time window (in seconds) used by the governor to average hashrate,
    # temperature, power, and hardware error percentage metrics, preventing
    # false-positive throttling from transient dips/peaks. Set to 0 to disable
    # and use instantaneous values.
    hashrate_average_window_seconds: int = 30

    # ---- Control thresholds (the friend's field-tested values). ----
    # VR temperature is the primary lever: nothing else in MinerWatch
    # governs it in a closed loop (the fan PID watches the chip, the
    # watchdog watches the chip). 67–70 °C is the hysteresis deadband (3 °C):
    # narrow enough to settle close to the limit, wide enough not to hunt.
    vr_high_c: float = 70.0           # above → step frequency down
    vr_low_c: float = 67.0            # below → step frequency up (recover)
    # Chip (ASIC) thresholds — used when a miner's Guardian temperature source
    # is set to "chip" instead of the default "vr". 60 °C is the usual Bitaxe
    # chip target, so this governs the chip toward the same point the auto-fan
    # PID already aims for: once the fan saturates and the chip drifts above
    # 60 °C the Guardian trims frequency, recovering below 57 °C. Same 3 °C
    # hysteresis deadband as the VR, with the 75 °C overheat watchdog still
    # underneath as the hard net. A per-miner ``guardian_max_temp_c`` overrides
    # the high point.
    chip_high_c: float = 60.0         # above → step frequency down
    chip_low_c: float = 57.0          # below → step frequency up (recover)
    # BitForge (forge-os) VR band. The 67–70 °C default above is tuned for
    # the Bitaxe's 5 V-input regulator; the BitForge Nano's TPS546A24
    # (12 V input) sits at ~71 °C at STOCK settings and its own firmware
    # only throttles the VR at 105 °C (TPS546_THROTTLE_TEMP; abs max
    # 145 °C) — governing it to 70 °C would pin the board below stock
    # frequency forever. 77–80 °C keeps the same 3 °C deadband, stays
    # under the co-tuner's 85 °C vr_cutoff_c hard net, and leaves 25 °C
    # of margin to the firmware's own throttle. Chip thresholds are NOT
    # forked: the BM1370 is the same ASIC as the Bitaxe Gamma's.
    bitforge_vr_high_c: float = 80.0  # above → step frequency down
    bitforge_vr_low_c: float = 77.0   # below → step frequency up (recover)
    # Rejected-share % over the interval = Δrejected / Δ(accepted+rejected)
    # × 100. This replaces the old errorCount/total HW% which was bogus on
    # AxeOS (its hashrateMonitor `total` is the hashrate, not a work counter,
    # so the ratio blew past 100%). Reject rate is monotonic, in the right
    # ballpark (well under 1% on a healthy miner) and available on every
    # AxeOS family (Bitaxe and Nerd*).
    reject_pct_max: float = 1.1
    # Statistical guard: only trust the reject % when at least this many
    # shares landed in the interval, so a single stale share on a quiet
    # window can't spike the rate and trigger a needless throttle.
    reject_min_shares: int = 20

    # ---- Step sizes (MHz). Asymmetric on purpose: back off fast, ----
    # recover gently, so the loop settles instead of hunting at the edges.
    step_down_vr_mhz: int = 20
    step_down_err_mhz: int = 10
    step_up_mhz: int = 10

    # ---- Instability brake: effective-hashrate validity (theoretical). ----
    # The reject-% term only catches POOL rejects; it misses ASIC hardware
    # errors (invalid nonces) that crater *effective* hashrate without ever
    # being submitted to the pool. Worse, an unstable chip does less real work,
    # draws less power and runs COOLER, which the recovery branch reads as "more
    # headroom" and pushes frequency even higher — a runaway. We close that gap
    # with a physics-based test: the theoretical hashrate for a frequency is
    # ``freq_mhz * small_core_count * asic_count / 1e6`` TH/s; a point is valid
    # only if the measured hashrate is at least ``valid_pct`` of it. Below that
    # the chip isn't keeping up → step down and pin a soft ceiling just below;
    # and frequency is never pushed UP unless the current point is valid. 0.97 =
    # 3% tolerance: tighter than the bitaxe benchmark's 6% so a *continuous*
    # governor stays nearer the efficient edge instead of pushing frequency into
    # rising errors. See docs/guardian-cotuner-design.md.
    valid_pct: float = 0.97
    # ASIC hardware-error % (AxeOS ``errorPercentage``) above which the chip is
    # treated as unstable even when the hashrate still meets ``valid_pct``: the
    # co-tuner raises voltage to cure it (frequency-only mode steps down). Catches
    # the "errors climbing while pushing frequency at fixed voltage" regime early,
    # before effective hashrate visibly drops.
    error_pct_max: float = 2.0
    step_down_hashrate_mhz: int = 20
    # Ignore the hashrate reading for this long after a frequency/voltage change:
    # AxeOS reports an EWMA that lags a minute or two, so comparing too soon
    # would misread the settling lag as a drop. 180 s matches field tuners.
    hashrate_settle_seconds: int = 180

    # Hard frequency floor (MHz): the governor never throttles below this, so a
    # runaway down-spiral can't drive a miner into uselessness. 485 sits just
    # below the Gamma's 525 stock — a little room to back off, but not into
    # pointless territory (mrv777's benchmark floors at 400; the terminally-
    # challenged tuner never goes below the 525 stock). A per-miner
    # ``guardian_freq_floor_mhz`` overrides this when set.
    frequency_floor_mhz: int = 485

    # Minimum seconds between two changes on the same miner. 0 = rely on
    # ``interval_seconds`` alone (each tick already leaves a full interval
    # for the VR to respond). Raise it to force extra settle time.
    cooldown_seconds: int = 0

    # ---- Phase 2: voltage co-tuning (the V/F co-tuner). ----
    # AxeOS applies voltage changes live too, which opens a second lever: cure
    # undervolt instability by RAISING coreVoltage instead of only cutting freq,
    # and LOWER it alongside frequency when shedding heat (stay near Vmin, better
    # J/TH). Auto-raising voltage 24/7 unattended is the riskiest lever, so it is
    # gated TWICE: this global master switch AND a per-miner opt-in
    # (``guardian_voltage_enabled``) behind a confirmation. ``v2_voltage_enabled``
    # is the master — flip it False to disable the voltage lever fleet-wide
    # regardless of per-miner opt-ins. See docs/guardian-cotuner-design.md.
    v2_voltage_enabled: bool = True
    v2_voltage_step_mv: int = 10
    v2_voltage_ceiling_mv: int = 1350
    v2_voltage_floor_mv: int = 1000

    # ---- Hard safety cutoffs (checked every tick when voltage co-tuning). ----
    # If any is hit the governor backs BOTH levers off (drop V and F) at once.
    # The chip cutoff sits below the 75 °C overheat watchdog so we act first; the
    # VR can run hotter than the user's soft limit; power falls back here when the
    # firmware doesn't report its own ``maxPower``; Vin guards a sagging PSU.
    chip_cutoff_c: float = 70.0
    vr_cutoff_c: float = 85.0
    power_cutoff_w: float = 40.0
    vin_min_mv: float = 4800.0
    vin_max_mv: float = 5500.0

    def temp_band(
        self, source: str | None, family: str | None = None
    ) -> tuple[float, float]:
        """``(high_c, low_c)`` default thresholds for a temperature source.

        ``source`` is the per-miner ``guardian_temp_source`` ("vr" | "chip").
        Anything other than "chip" (including ``None`` / unknown) falls back to
        the VR band, so an unset value behaves exactly like before this knob
        existed — VR-governed. The gap between the two is the hysteresis
        deadband, reused to derive the recovery point when a miner overrides
        only the high threshold via ``guardian_max_temp_c``.

        ``family`` selects per-board VR defaults where the Bitaxe-tuned band
        would mis-govern: the BitForge's TPS546 runs ~71 °C at stock, so it
        gets the ``bitforge_vr_*`` band. The chip band is family-agnostic
        (same ASICs across the AxeOS families).
        """
        if str(source or "").lower() == "chip":
            return self.chip_high_c, self.chip_low_c
        if str(family or "").lower() == "bitforge":
            return self.bitforge_vr_high_c, self.bitforge_vr_low_c
        return self.vr_high_c, self.vr_low_c


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    polling: PollingCfg = field(default_factory=PollingCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    auth: AuthCfg = field(default_factory=AuthCfg)
    guardian: GuardianCfg = field(default_factory=GuardianCfg)

    @classmethod
    def load(cls) -> "Config":
        candidates = [ROOT_DIR / "config.yaml", ROOT_DIR / "config.example.yaml"]
        raw: dict[str, Any] = {}
        for path in candidates:
            if path.exists():
                with path.open("r", encoding="utf-8") as fp:
                    raw = yaml.safe_load(fp) or {}
                break
        return cls(
            server=ServerCfg(**raw.get("server", {})),
            network=NetworkCfg(**raw.get("network", {})),
            polling=PollingCfg(**raw.get("polling", {})),
            storage=StorageCfg(**raw.get("storage", {})),
            alerts=AlertsCfg(**raw.get("alerts", {})),
            auth=AuthCfg(**raw.get("auth", {})),
            guardian=GuardianCfg(**raw.get("guardian", {})),
        )

    def apply_overrides(self, overrides: dict[str, Any]) -> None:
        """Apply overrides read from the DB (runtime settings)."""
        applied: set[str] = set()
        for key, value in overrides.items():
            if "." not in key:
                continue
            section, field_name = key.split(".", 1)
            section_obj = getattr(self, section, None)
            if section_obj is None:
                continue
            if not hasattr(section_obj, field_name):
                continue
            current = getattr(section_obj, field_name)
            try:
                if isinstance(current, bool):
                    coerced = str(value).lower() in {"1", "true", "yes", "on"}
                elif isinstance(current, int) and not isinstance(current, bool):
                    coerced = int(value)
                elif isinstance(current, float):
                    coerced = float(value)
                else:
                    coerced = value
                setattr(section_obj, field_name, coerced)
                applied.add(key)
            except (TypeError, ValueError):
                continue

        # Backward compat: if the legacy `storage.retention_days` was the
        # only retention knob set, mirror it onto the new 1m tier so the
        # user's existing setting keeps having the effect they expect.
        if (
            "storage.retention_days" in applied
            and "storage.retention_1m_days" not in applied
        ):
            self.storage.retention_1m_days = int(self.storage.retention_days)


_config: Config | None = None


def get_config() -> Config:
    global _config
    if _config is None:
        DATA_DIR.mkdir(exist_ok=True)
        # The data dir holds secrets: the VAPID private key, and the auth
        # password (stored in the settings DB). Restrict it to the owner so
        # other local users can't read them. Best-effort — some mounts
        # (read-only, SMB/CIFS) reject chmod, and we must not crash over it.
        try:
            DATA_DIR.chmod(0o700)
        except OSError:
            pass
        _config = Config.load()
    return _config


def reload_config() -> Config:
    global _config
    _config = None
    return get_config()


# Path helpers
def db_path() -> Path:
    return DATA_DIR / "minerwatch.db"


def vapid_keys_path() -> Path:
    return DATA_DIR / "vapid_keys.json"

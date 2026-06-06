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

    # ---- Control thresholds (the friend's field-tested values). ----
    # VR temperature is the primary lever: nothing else in MinerWatch
    # governs it in a closed loop (the fan PID watches the chip, the
    # watchdog watches the chip). 65–70 °C is the hysteresis deadband.
    vr_high_c: float = 70.0           # above → step frequency down
    vr_low_c: float = 65.0            # below → step frequency up (recover)
    # Chip (ASIC) thresholds — used when a miner's Guardian temperature source
    # is set to "chip" instead of the default "vr". 60 °C is the usual Bitaxe
    # chip target, so this governs the chip toward the same point the auto-fan
    # PID already aims for: once the fan saturates and the chip drifts above
    # 60 °C the Guardian trims frequency, recovering below 55 °C. Same 5 °C
    # hysteresis deadband as the VR, with the 75 °C overheat watchdog still
    # underneath as the hard net. A per-miner ``guardian_max_temp_c`` overrides
    # the high point.
    chip_high_c: float = 60.0         # above → step frequency down
    chip_low_c: float = 55.0          # below → step frequency up (recover)
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

    # Hard frequency floor (MHz): the governor never throttles below this,
    # so a runaway down-spiral can't drive a miner into uselessness. A
    # per-miner ``guardian_freq_floor_mhz`` overrides this when set.
    frequency_floor_mhz: int = 400

    # Minimum seconds between two changes on the same miner. 0 = rely on
    # ``interval_seconds`` alone (each tick already leaves a full interval
    # for the VR to respond). Raise it to force extra settle time.
    cooldown_seconds: int = 0

    # ---- v2 (NOT active in v1; documented here for when we wire it). ----
    # AxeOS applies voltage changes live too, which opens a second lever:
    #   * respond to a sustained reject rate by RAISING coreVoltage (the
    #     proper fix for undervolt instability) instead of only cutting freq;
    #   * when cutting frequency, optionally LOWER coreVoltage in step to
    #     stay near Vmin and preserve J/TH efficiency.
    # Auto-raising voltage 24/7 unattended is riskier (more heat/watts,
    # closer to hardware limits), so it stays out of v1. The knobs below are
    # placeholders kept inert until v2 reads them. See guardian-design.md §v2.
    v2_voltage_enabled: bool = False
    v2_voltage_step_mv: int = 10
    v2_voltage_ceiling_mv: int = 1300
    v2_voltage_floor_mv: int = 1000

    def temp_band(self, source: str | None) -> tuple[float, float]:
        """``(high_c, low_c)`` default thresholds for a temperature source.

        ``source`` is the per-miner ``guardian_temp_source`` ("vr" | "chip").
        Anything other than "chip" (including ``None`` / unknown) falls back to
        the VR band, so an unset value behaves exactly like before this knob
        existed — VR-governed. The gap between the two is the hysteresis
        deadband, reused to derive the recovery point when a miner overrides
        only the high threshold via ``guardian_max_temp_c``.
        """
        if str(source or "").lower() == "chip":
            return self.chip_high_c, self.chip_low_c
        return self.vr_high_c, self.vr_low_c


@dataclass
class MqttCfg:
    """Optional MQTT publisher — Home Assistant discovery + flat topics.

    MinerWatch is an MQTT *client*: it connects to a broker the operator
    points it at (e.g. the Mosquitto add-on), it never runs its own. The
    whole feature self-disables when ``enabled`` is False or the ``aiomqtt``
    dependency is missing, mirroring the log_streamer pattern, so a missing
    optional dep never breaks the app.

    See ``docs/home-assistant-integration.md`` for the full design (topic
    schema, discovery payloads, the ESP32/ESPHome panel, security notes).
    """

    enabled: bool = False
    host: str = ""               # broker IP/hostname, e.g. localhost
    port: int = 1883
    username: str = ""
    password: str = ""           # stored like other secrets — see security-review.md F3
    base_topic: str = "minerwatch"
    discovery_prefix: str = "homeassistant"
    qos: int = 1
    retain: bool = True
    # Publish Home Assistant MQTT-discovery configs so miners auto-appear
    # as HA devices/entities. Turn off if you only consume the raw/flat
    # topics (e.g. a standalone ESPHome panel) and don't want HA noise.
    discovery_enabled: bool = True
    # Also publish scalar per-field topics minerwatch/<mac>/f/<field>, handy
    # for constrained consumers (ESP32/ESPHome) that can't parse JSON on-device.
    publish_flat_topics: bool = False
    # Expose write/command entities (restart/fan/frequency/voltage). OFF by
    # default: these are destructive and interact with Guardian/auto-fan.
    allow_controls: bool = False
    # 0 = publish on every poll; >0 = throttle to at most once per N seconds.
    publish_interval_s: int = 0
    tls: bool = False            # use TLS (port is usually 8883 then)
    # Optional: relay an ambient temperature from another device on the same
    # broker into the ESP32 panel feed. Subscribe to a plain-text Celsius topic
    # (e.g. a sensor publishing "23.5"); MinerWatch computes a 60s moving
    # average + session min/max and ships them in the panel blob, so the panel
    # needs no extra wiring. Empty = feature disabled. The status topic
    # (online/offline) is optional and only used for availability.
    ambient_temp_topic: str = ""
    ambient_temp_status_topic: str = ""


@dataclass
class Config:
    server: ServerCfg = field(default_factory=ServerCfg)
    network: NetworkCfg = field(default_factory=NetworkCfg)
    polling: PollingCfg = field(default_factory=PollingCfg)
    storage: StorageCfg = field(default_factory=StorageCfg)
    alerts: AlertsCfg = field(default_factory=AlertsCfg)
    auth: AuthCfg = field(default_factory=AuthCfg)
    guardian: GuardianCfg = field(default_factory=GuardianCfg)
    mqtt: MqttCfg = field(default_factory=MqttCfg)

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
            mqtt=MqttCfg(**raw.get("mqtt", {})),
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

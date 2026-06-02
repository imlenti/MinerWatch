# SPDX-License-Identifier: AGPL-3.0-only
"""Optional MQTT publisher — Home Assistant discovery + flat topics.

MinerWatch is an MQTT *client*: it connects to a broker the operator points
it at (e.g. the Mosquitto add-on); it never runs its own. After every poll
cycle the publisher pushes one retained JSON state blob per miner (for Home
Assistant) and, optionally, scalar per-field topics (for constrained
consumers such as an ESP32/ESPHome panel). It can also publish HA
MQTT-discovery configs so miners auto-appear as HA devices/entities, and —
when explicitly allowed — subscribe to command topics to control miners.

The whole module self-disables when ``mqtt.enabled`` is False or the
``aiomqtt`` dependency is missing, mirroring the log_streamer pattern so a
missing optional dependency never breaks the app.

Full design + topic/payload schemas: ``docs/home-assistant-integration.md``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from . import btc_price
from .config import get_config
from .miners import driver_for_record, get_driver
from .miners.base import MinerSample

try:  # optional dependency — feature self-disables if absent
    import aiomqtt
except Exception:  # noqa: BLE001 - ImportError or transitive import failure
    aiomqtt = None  # type: ignore[assignment]

log = logging.getLogger("minerwatch.mqtt")


# ---------------------------------------------------------------------------
# Pure payload-building helpers (no I/O — unit-testable in isolation)
# ---------------------------------------------------------------------------

# Fields copied verbatim from a MinerSample into the JSON state blob.
STATE_FIELDS: tuple[str, ...] = (
    "hashrate_ths", "power_w", "efficiency_w_per_ths",
    "temp_chip_c", "temp_chip_2_c", "temp_vr_c",
    "temp_outlet_c", "temp_inlet_c", "temp_avg_c",
    "fan_rpm", "fan_pct", "fan_rpm_2", "fan_pct_2",
    "frequency_mhz", "voltage_mv", "current_a",
    "chip_count", "board_count",
    "hw_errors", "hw_error_rate",
    "uptime_s", "accepted", "rejected",
    "best_difficulty", "best_difficulty_alltime", "network_difficulty",
    "pool_url", "worker", "pool_active",
)

# Sensor entity map (grounded in backend/miners/base.py MinerSample).
# (object_id, json_field, name, device_class, unit, state_class, icon, entity_category)
SENSORS: tuple[tuple[Any, ...], ...] = (
    ("hashrate", "hashrate_ths", "Hashrate", None, "TH/s", "measurement", "mdi:pickaxe", None),
    ("power", "power_w", "Power", "power", "W", "measurement", None, None),
    ("efficiency", "efficiency_w_per_ths", "Efficiency", None, "W/TH", "measurement", "mdi:lightning-bolt", None),
    ("temp_chip", "temp_chip_c", "Chip temp", "temperature", "°C", "measurement", None, None),
    ("temp_vr", "temp_vr_c", "VR temp", "temperature", "°C", "measurement", None, None),
    ("fan_rpm", "fan_rpm", "Fan RPM", None, "rpm", "measurement", "mdi:fan", None),
    ("fan_pct", "fan_pct", "Fan", None, "%", "measurement", "mdi:fan", None),
    ("frequency", "frequency_mhz", "Frequency", "frequency", "MHz", "measurement", None, None),
    ("voltage", "voltage_mv", "Core voltage", "voltage", "mV", "measurement", None, None),
    ("current", "current_a", "Current", "current", "A", "measurement", None, "diagnostic"),
    ("uptime", "uptime_s", "Uptime", "duration", "s", None, None, "diagnostic"),
    ("accepted", "accepted", "Accepted shares", None, "shares", "total_increasing", "mdi:check", "diagnostic"),
    ("rejected", "rejected", "Rejected shares", None, "shares", "total_increasing", "mdi:close", "diagnostic"),
    ("best_diff", "best_difficulty", "Best difficulty", None, None, "measurement", "mdi:trophy", None),
    ("hw_err_rate", "hw_error_rate", "HW error rate", None, "%", "measurement", None, "diagnostic"),
    ("pool", "pool_url", "Pool", None, None, None, "mdi:server-network", "diagnostic"),
)

# Command entities. Gated by mqtt.allow_controls AND the driver capability flag.
# (object_id, component, action, capability_attr, extra_keys)
COMMANDS: tuple[tuple[Any, ...], ...] = (
    ("restart", "button", "restart", "can_restart", {"device_class": "restart", "payload_press": "RESTART"}),
    ("fan", "number", "fan", "can_set_fan", {"min": 0, "max": 100, "step": 1, "unit_of_measurement": "%", "icon": "mdi:fan"}),
    # Generic bounds — the design doc notes these should ideally be clamped to
    # the miner's advertised min/max; broad-but-safe defaults for v1.
    ("frequency", "number", "frequency", "can_set_frequency", {"min": 100, "max": 1000, "step": 5, "unit_of_measurement": "MHz"}),
    ("voltage", "number", "voltage", "can_set_voltage", {"min": 900, "max": 1400, "step": 5, "unit_of_measurement": "mV"}),
)


def sanitize_mac(mac: Any, miner_id: Any) -> str:
    """Return a topic/identifier-safe id derived from the MAC.

    HA discovery components must match [a-zA-Z0-9_-]; MAC colons are
    stripped. Falls back to ``mw<db_id>`` when no MAC is known.
    """
    if mac:
        cleaned = re.sub(r"[^0-9a-zA-Z]", "", str(mac)).lower()
        if cleaned:
            return cleaned
    return f"mw{miner_id}"


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def state_payload(sample: MinerSample | None, rec: dict | None) -> dict[str, Any]:
    """Build the JSON blob published to ``<base>/<mac>/state``."""
    out: dict[str, Any] = {}
    if sample is not None:
        for f in STATE_FIELDS:
            out[f] = getattr(sample, f, None)
        out["status"] = "online" if getattr(sample, "online", False) else "offline"
    else:
        out["status"] = "offline"
    if rec is not None:
        out["fan_mode"] = rec.get("fan_mode")
    return out


def flat_pairs(base: str, payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Scalar per-field topics for non-null values: (topic, value)."""
    return [
        (f"{base}/f/{k}", _scalar(v))
        for k, v in payload.items()
        if v is not None
    ]


def device_block(rec: dict | None, sample: MinerSample | None, mac_id: str) -> dict[str, Any]:
    name = None
    if rec:
        name = rec.get("name")
    if not name and sample is not None:
        name = getattr(sample, "hostname", None)
    name = name or mac_id

    model = None
    if sample is not None:
        model = sample.model or getattr(sample, "chip_model", None)
    if not model and rec:
        model = rec.get("model") or rec.get("family")

    sw = getattr(sample, "firmware_version", None) if sample is not None else None

    block: dict[str, Any] = {
        "identifiers": [f"minerwatch_{mac_id}"],
        "name": name,
        "manufacturer": "MinerWatch",
        "model": model or "miner",
        "via_device": "minerwatch_bridge",
    }
    if sw:
        block["sw_version"] = sw
    return block


def device_signature(rec: dict | None, sample: MinerSample | None, mac_id: str) -> str:
    """Cheap signature: re-publish discovery only when device metadata changes."""
    blk = device_block(rec, sample, mac_id)
    return json.dumps([blk.get("name"), blk.get("model"), blk.get("sw_version")], sort_keys=True)


def _num(value: Any) -> float | None:
    """Coerce to a rounded float for the compact panel feed, else None."""
    try:
        return None if value is None else round(float(value), 2)
    except (TypeError, ValueError):
        return None


def panel_feed(
    miners: list[dict],
    samples: dict[int, MinerSample],
    btc_usd: float | None = None,
    btc_at: str | None = None,
) -> dict[str, Any]:
    """Consolidated single-topic blob for a constrained display (ESPHome panel).

    One compact JSON object, one entry per miner, with name/model resolved the
    same way as HA discovery. Lets the panel use a SINGLE subscription and adapt
    to the fleet automatically. Published retained to ``<base>/panel``. Keys are
    short to keep the payload small for an ESP32: id, name, ip, model,
    hr (TH/s), pw (W), tp (chip C), vr (VR C), on (online bool).

    Optionally carries a Bitcoin spot price for the panel's price screen:
    ``btc_usd`` (integer USD) and ``btc_at`` (a preformatted "YYYY-MM-DD HH:MM"
    stamp in the server's local time). Both are omitted when no price is
    available, so the panel simply shows "--" until one arrives. Formatting the
    stamp here keeps the firmware free of any clock/timezone setup.
    """
    out: list[dict[str, Any]] = []
    for rec in miners:
        mac_id = sanitize_mac(rec.get("mac"), rec.get("id"))
        sample = samples.get(int(rec["id"])) if rec.get("id") is not None else None
        dev = device_block(rec, sample, mac_id)
        ip = (
            getattr(sample, "host", None)
            or rec.get("host") or rec.get("ip") or rec.get("address") or ""
        )
        out.append(
            {
                "id": mac_id,
                "name": dev.get("name") or mac_id,
                "ip": ip,
                "model": dev.get("model") or "",
                "hr": _num(getattr(sample, "hashrate_ths", None)),
                "pw": _num(getattr(sample, "power_w", None)),
                "tp": _num(getattr(sample, "temp_chip_c", None)),
                "vr": _num(getattr(sample, "temp_vr_c", None)),
                "on": bool(sample and getattr(sample, "online", False)),
            }
        )
    blob: dict[str, Any] = {"miners": out}
    if btc_usd is not None:
        blob["btc_usd"] = round(float(btc_usd))
    if btc_at:
        blob["btc_at"] = btc_at
    return blob


def _capabilities(family: str) -> Any:
    try:
        return get_driver(family)
    except Exception:  # noqa: BLE001 - unknown family
        return None


def discovery_configs(
    cfg: Any, rec: dict | None, sample: MinerSample | None, mac_id: str
) -> list[tuple[str, dict[str, Any]]]:
    """Build (topic, payload) discovery configs for one miner's entities."""
    base = f"{cfg.base_topic}/{mac_id}"
    state_topic = f"{base}/state"
    availability = [
        {"topic": f"{cfg.base_topic}/bridge/availability"},
        {"topic": f"{base}/availability"},
    ]
    device = device_block(rec, sample, mac_id)
    origin = {"name": "MinerWatch", "support_url": "https://github.com/imlenti/MinerWatch"}

    def _disc_topic(component: str, object_id: str) -> str:
        return f"{cfg.discovery_prefix}/{component}/minerwatch_{mac_id}/{object_id}/config"

    items: list[tuple[str, dict[str, Any]]] = []

    for object_id, field, name, dev_class, unit, state_class, icon, ent_cat in SENSORS:
        payload: dict[str, Any] = {
            "name": name,
            "unique_id": f"minerwatch_{mac_id}_{object_id}",
            "state_topic": state_topic,
            "value_template": "{{ value_json." + field + " }}",
            "availability": availability,
            "availability_mode": "all",
            "device": device,
            "origin": origin,
        }
        if dev_class:
            payload["device_class"] = dev_class
        if unit:
            payload["unit_of_measurement"] = unit
        if state_class:
            payload["state_class"] = state_class
        if icon:
            payload["icon"] = icon
        if ent_cat:
            payload["entity_category"] = ent_cat
        items.append((_disc_topic("sensor", object_id), payload))

    # Online/offline as a connectivity binary_sensor (derived from the poller).
    items.append((
        _disc_topic("binary_sensor", "status"),
        {
            "name": "Status",
            "unique_id": f"minerwatch_{mac_id}_status",
            "state_topic": state_topic,
            "value_template": "{{ value_json.status }}",
            "payload_on": "online",
            "payload_off": "offline",
            "device_class": "connectivity",
            "availability": [{"topic": f"{cfg.base_topic}/bridge/availability"}],
            "device": device,
            "origin": origin,
        },
    ))

    # Command entities — only when allowed AND the family supports the action.
    if cfg.allow_controls and rec is not None:
        caps = _capabilities(rec.get("family", ""))
        for object_id, component, action, cap_attr, extra in COMMANDS:
            if caps is None or not getattr(caps, cap_attr, False):
                continue
            payload = {
                "name": object_id.capitalize(),
                "unique_id": f"minerwatch_{mac_id}_cmd_{object_id}",
                "command_topic": f"{base}/cmd/{action}",
                "availability": [{"topic": f"{cfg.base_topic}/bridge/availability"}],
                "entity_category": "config",
                "device": device,
                "origin": origin,
            }
            if component == "number":
                payload["state_topic"] = state_topic
                # map command object_id back to the matching state field
                field_for = {"fan": "fan_pct", "frequency": "frequency_mhz", "voltage": "voltage_mv"}
                payload["value_template"] = "{{ value_json." + field_for[object_id] + " }}"
            payload.update(extra)
            items.append((_disc_topic(component, object_id), payload))

    return items


def discovery_topics(cfg: Any, mac_id: str) -> list[str]:
    """All possible discovery config topics for a miner (for removal)."""
    topics = [
        f"{cfg.discovery_prefix}/sensor/minerwatch_{mac_id}/{object_id}/config"
        for object_id, *_ in SENSORS
    ]
    topics.append(f"{cfg.discovery_prefix}/binary_sensor/minerwatch_{mac_id}/status/config")
    for object_id, component, *_ in COMMANDS:
        topics.append(f"{cfg.discovery_prefix}/{component}/minerwatch_{mac_id}/{object_id}/config")
    return topics


def bridge_discovery(cfg: Any) -> tuple[str, dict[str, Any]]:
    """Anchor device for the MinerWatch bridge (so via_device resolves)."""
    topic = f"{cfg.discovery_prefix}/binary_sensor/minerwatch_bridge/status/config"
    payload = {
        "name": "MinerWatch bridge",
        "unique_id": "minerwatch_bridge_status",
        "state_topic": f"{cfg.base_topic}/bridge/availability",
        "payload_on": "online",
        "payload_off": "offline",
        "device_class": "connectivity",
        "device": {
            "identifiers": ["minerwatch_bridge"],
            "name": "MinerWatch",
            "manufacturer": "MinerWatch",
            "model": "Bridge",
        },
        "origin": {"name": "MinerWatch"},
    }
    return topic, payload


# ---------------------------------------------------------------------------
# Publisher (connection lifecycle + I/O)
# ---------------------------------------------------------------------------

class MqttPublisher:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._client: Any = None  # aiomqtt.Client when connected, else None
        # mac_id -> last device signature (re-publish discovery only on change)
        self._known: dict[str, str] = {}
        # mac_id -> miner record (for command dispatch)
        self._miners_by_mac: dict[str, dict] = {}
        self._last_publish = 0.0

    # ---- lifecycle ----

    async def start(self) -> None:
        cfg = get_config().mqtt
        if not cfg.enabled:
            log.info("MQTT disabled (mqtt.enabled=false) — publisher not started")
            return
        if aiomqtt is None:
            log.warning(
                "MQTT enabled but the 'aiomqtt' package is not installed — "
                "publisher disabled. Run: pip install aiomqtt"
            )
            return
        if not cfg.host:
            log.warning("MQTT enabled but mqtt.host is empty — publisher disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="minerwatch-mqtt")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()
        self._task = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    # ---- connection loop ----

    async def _run(self) -> None:
        cfg = get_config().mqtt
        bridge_avail = f"{cfg.base_topic}/bridge/availability"
        backoff = 1.0
        while not self._stop.is_set():
            try:
                will = aiomqtt.Will(bridge_avail, payload="offline", qos=cfg.qos, retain=True)
                kwargs: dict[str, Any] = dict(hostname=cfg.host, port=int(cfg.port), will=will)
                if cfg.username:
                    kwargs["username"] = cfg.username
                if cfg.password:
                    kwargs["password"] = cfg.password
                if cfg.tls:
                    kwargs["tls_params"] = aiomqtt.TLSParameters()

                async with aiomqtt.Client(**kwargs) as client:
                    self._client = client
                    backoff = 1.0
                    log.info("MQTT connected to %s:%s", cfg.host, cfg.port)
                    await client.publish(bridge_avail, "online", qos=cfg.qos, retain=True)
                    if cfg.discovery_enabled:
                        topic, payload = bridge_discovery(cfg)
                        await client.publish(topic, json.dumps(payload), qos=cfg.qos, retain=True)
                    # Force discovery re-publish for all miners after (re)connect.
                    self._known.clear()
                    if cfg.allow_controls:
                        await client.subscribe(f"{cfg.base_topic}/+/cmd/+", qos=cfg.qos)

                    # Keepalive + command loop, interruptible by stop().
                    stop_task = asyncio.ensure_future(self._stop.wait())
                    msg_task = asyncio.ensure_future(self._message_loop(client))
                    done, pending = await asyncio.wait(
                        {stop_task, msg_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for t in pending:
                        t.cancel()
                    if self._stop.is_set():
                        # Clean shutdown: flip availability before disconnecting.
                        try:
                            await client.publish(bridge_avail, "offline", qos=cfg.qos, retain=True)
                        except Exception:  # noqa: BLE001
                            pass
                    if msg_task in done:
                        msg_task.result()  # re-raise MqttError (dropped connection)
            except aiomqtt.MqttError as exc:
                log.warning("MQTT connection error: %s — retry in %.0fs", exc, backoff)
            except Exception:  # noqa: BLE001
                log.exception("MQTT loop error — retry in %.0fs", backoff)
            finally:
                self._client = None

            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
        log.info("MQTT publisher stopped")

    async def _message_loop(self, client: Any) -> None:
        """Iterate inbound messages. Also surfaces disconnects as MqttError."""
        async for message in client.messages:
            try:
                await self._handle_command(message)
            except Exception:  # noqa: BLE001
                log.exception("MQTT command handling error")

    # ---- publishing (called from the poller) ----

    async def publish_fleet(
        self, miners: list[dict], samples: dict[int, MinerSample]
    ) -> None:
        """Publish state (+ optional flat topics + discovery) for the fleet.

        No-op unless connected. Never raises into the poll loop.
        """
        client = self._client
        if client is None:
            return
        cfg = get_config().mqtt

        now = time.monotonic()
        if cfg.publish_interval_s and (now - self._last_publish) < cfg.publish_interval_s:
            return
        self._last_publish = now

        by_mac: dict[str, dict] = {}
        try:
            for rec in miners:
                mac_id = sanitize_mac(rec.get("mac"), rec.get("id"))
                by_mac[mac_id] = rec
                sample = samples.get(int(rec["id"]))
                await self._publish_one(client, cfg, rec, sample, mac_id)
            self._miners_by_mac = by_mac
            if cfg.publish_flat_topics:
                # Single consolidated blob for the ESPHome panel (adaptive UI).
                # Piggyback a BTC/USD spot price (+ "as of" stamp) so the panel's
                # price screen needs no on-device HTTPS. Soft-fail: a None price
                # just omits the fields and the panel shows "--".
                btc = await btc_price.get_btc_usd()
                btc_at = None
                if btc is not None:
                    ts = btc_price.last_fetched_ts()
                    if ts:
                        btc_at = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
                await client.publish(
                    f"{cfg.base_topic}/panel",
                    json.dumps(panel_feed(miners, samples, btc_usd=btc, btc_at=btc_at)),
                    qos=cfg.qos, retain=cfg.retain,
                )
            if cfg.discovery_enabled:
                await self._reconcile_removed(client, cfg, set(by_mac))
        except aiomqtt.MqttError as exc:
            log.warning("MQTT publish failed: %s", exc)
        except Exception:  # noqa: BLE001
            log.exception("MQTT publish error")

    async def _publish_one(
        self, client: Any, cfg: Any, rec: dict, sample: MinerSample | None, mac_id: str
    ) -> None:
        base = f"{cfg.base_topic}/{mac_id}"
        online = bool(sample and getattr(sample, "online", False))
        await client.publish(
            f"{base}/availability", "online" if online else "offline",
            qos=cfg.qos, retain=cfg.retain,
        )
        payload = state_payload(sample, rec)
        await client.publish(f"{base}/state", json.dumps(payload), qos=cfg.qos, retain=cfg.retain)

        if cfg.publish_flat_topics:
            for topic, value in flat_pairs(base, payload):
                await client.publish(topic, value, qos=cfg.qos, retain=cfg.retain)

        if cfg.discovery_enabled:
            sig = device_signature(rec, sample, mac_id)
            if self._known.get(mac_id) != sig:
                for topic, dpayload in discovery_configs(cfg, rec, sample, mac_id):
                    await client.publish(topic, json.dumps(dpayload), qos=cfg.qos, retain=True)
                self._known[mac_id] = sig

    async def _reconcile_removed(self, client: Any, cfg: Any, current_macs: set[str]) -> None:
        """Delete discovery for miners that disappeared (publish empty retained)."""
        gone = set(self._known) - current_macs
        for mac_id in gone:
            for topic in discovery_topics(cfg, mac_id):
                await client.publish(topic, "", qos=cfg.qos, retain=True)
            await client.publish(
                f"{cfg.base_topic}/{mac_id}/availability", "offline",
                qos=cfg.qos, retain=cfg.retain,
            )
            self._known.pop(mac_id, None)

    # ---- command dispatch ----

    async def _handle_command(self, message: Any) -> None:
        cfg = get_config().mqtt
        if not cfg.allow_controls:
            return
        topic = str(message.topic)
        parts = topic.split("/")
        # expected: <base>/<mac>/cmd/<action>
        if len(parts) < 4 or parts[-2] != "cmd":
            return
        mac_id, action = parts[-3], parts[-1]
        raw = message.payload
        payload = raw.decode("utf-8", "ignore").strip() if isinstance(raw, (bytes, bytearray)) else str(raw)

        rec = self._miners_by_mac.get(mac_id)
        if not rec:
            log.warning("MQTT command for unknown miner %s (topic=%s)", mac_id, topic)
            return

        timeout = get_config().polling.request_timeout
        driver = driver_for_record({**rec, "timeout": timeout})
        try:
            if action == "restart" and getattr(driver, "can_restart", False):
                await driver.restart()
                log.info("MQTT command: restart %s", mac_id)
            elif action == "fan" and getattr(driver, "can_set_fan", False):
                await driver.set_fan_speed(int(float(payload)))
                log.info("MQTT command: fan=%s%% %s", payload, mac_id)
            elif action == "frequency" and getattr(driver, "can_set_frequency", False):
                await driver.set_frequency(int(float(payload)))
                log.info("MQTT command: frequency=%sMHz %s", payload, mac_id)
            elif action == "voltage" and getattr(driver, "can_set_voltage", False):
                await driver.set_voltage(int(float(payload)))
                log.info("MQTT command: voltage=%smV %s", payload, mac_id)
            else:
                log.warning("MQTT command ignored (unknown/unsupported): %s", topic)
        except (TypeError, ValueError):
            log.warning("MQTT command bad payload %r for %s", payload, topic)
        except Exception:  # noqa: BLE001
            log.exception("MQTT command execution failed: %s", topic)


async def test_connection(cfg: Any = None) -> tuple[bool, str]:
    """Try a short connect + publish to the configured broker.

    Returns ``(ok, detail)``. Uses the *currently stored* config, so the UI
    flow is "Save, then Test" (same convention as the Telegram test).
    """
    if aiomqtt is None:
        return False, "The 'aiomqtt' package is not installed on the server."
    cfg = cfg or get_config().mqtt
    if not cfg.host:
        return False, "No broker host configured — set mqtt.host first."

    kwargs: dict[str, Any] = dict(hostname=cfg.host, port=int(cfg.port))
    if cfg.username:
        kwargs["username"] = cfg.username
    if cfg.password:
        kwargs["password"] = cfg.password
    if cfg.tls:
        kwargs["tls_params"] = aiomqtt.TLSParameters()

    async def _attempt() -> None:
        async with aiomqtt.Client(**kwargs) as client:
            await client.publish(f"{cfg.base_topic}/bridge/test", "ok", qos=cfg.qos)

    try:
        await asyncio.wait_for(_attempt(), timeout=8)
    except asyncio.TimeoutError:
        return False, f"Timed out connecting to {cfg.host}:{cfg.port}."
    except Exception as exc:  # noqa: BLE001 - surface broker error to the UI
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"Connected to {cfg.host}:{cfg.port} and published a test message."


# Global instance (used by main.py and poller.py)
mqtt_publisher = MqttPublisher()

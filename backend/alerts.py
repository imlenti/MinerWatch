# SPDX-License-Identifier: AGPL-3.0-only
"""Alert system + Web Push (VAPID).

Logic:
- On every polling cycle :func:`evaluate` receives the current samples.
- For each miner we check thresholds (chip / VR temp) and online↔offline
  transitions (based on the timestamp of the last successful poll).
- If the state changed compared to the previous cycle, we log an alert
  in the DB and send a push notification to every registered client.

VAPID:
- Keys are generated on first startup at ``data/vapid_keys.json``
  (P-256 curve per RFC 8292).
- The public endpoint ``/api/push/vapid_public_key`` returns the public
  key the browser needs to subscribe.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from typing import Any

import asyncio

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

from .config import get_config, vapid_keys_path
from . import db
from .miners.base import MinerSample

log = logging.getLogger("minerwatch.alerts")

# In-memory state: for each miner, the last reported state/temperature,
# to avoid duplicating notifications every 5 seconds.
_state: dict[int, dict[str, Any]] = {}

# Anti-spam tuning for best-share notifications.
# - Skip the very first record we ever observe for a miner: a brand-new
#   miner sets a "best" on its very first share, which would just be noise.
# - Require the new value to beat the previous all-time by at least
#   BEST_SHARE_MIN_GROWTH. The user wants to see every small bump, so
#   we keep it at +0.1% (essentially "any improvement"). Bump back to
#   1.10 (+10%) if the home dashboard ever becomes too chatty.
# - Cool-down per miner: never push more than once every BEST_SHARE_COOLDOWN_S
#   seconds, no matter what. Acts as the real anti-flood at 0.1% growth.
BEST_SHARE_MIN_GROWTH = 1.001
BEST_SHARE_COOLDOWN_S = 60

# py-vapid validates this field with a strict regex: it must be
# "mailto:<something>@localhost" or "mailto:<something>@<domain.tld>".
# "@local" without a dot fails with "Missing 'sub' from claims".
VAPID_CLAIMS_EMAIL = "mailto:minerwatch@localhost"


# ---------- VAPID keys ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def ensure_vapid_keys() -> dict[str, str]:
    path = vapid_keys_path()
    if path.exists():
        try:
            keys = json.loads(path.read_text("utf-8"))
            # File already exists: make sure private_b64 is present
            # (older versions might only have private_pem).
            if "private_b64" in keys and "public_b64" in keys:
                return keys
        except (ValueError, OSError):
            pass

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key()

    priv_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    public_numbers = public_key.public_numbers()
    x = public_numbers.x.to_bytes(32, "big")
    y = public_numbers.y.to_bytes(32, "big")
    pub_b64 = _b64(b"\x04" + x + y)

    priv_int = private_key.private_numbers().private_value
    priv_b64 = _b64(priv_int.to_bytes(32, "big"))

    keys = {
        "private_pem": priv_pem,
        "private_b64": priv_b64,
        "public_b64": pub_b64,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    # Private key on disk — restrict to owner read/write. Best-effort: the
    # parent data dir is already locked to 0700, this just hardens the file
    # itself. Exotic mounts may reject chmod, so we don't crash over it.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    log.info("VAPID keys generated at %s", path)
    return keys


def public_key_b64() -> str:
    return ensure_vapid_keys()["public_b64"]


def private_key_pem() -> str:
    return ensure_vapid_keys()["private_pem"]


def private_key_b64() -> str:
    """Private key in urlsafe-base64 format (raw 32 bytes).

    Used instead of the PEM to dodge a LibreSSL bug on macOS:
    'header too long' when cryptography tries to re-parse the PEM.
    py-vapid accepts this form directly via Vapid01.from_raw().
    """
    return ensure_vapid_keys()["private_b64"]


# ---------- Push send ----------

async def send_push(payload: dict[str, Any]) -> None:
    import asyncio as _aio

    # Global kill-switch: if the user set notifications_enabled=False
    # in Settings, return immediately without sending anything. The
    # subscriptions stay in the DB so when re-enabled it just works
    # again without the user having to re-subscribe each browser.
    cfg = get_config()
    if not cfg.alerts.notifications_enabled:
        log.info(
            "push: notifications_enabled=False → notification '%s' silenced",
            payload.get("title", ""),
        )
        return

    subs = await db.list_push_subs()
    if not subs:
        log.info("push: no subscribers registered (no notification sent)")
        return
    # We use the RAW base64 key instead of the PEM: pywebpush feeds it
    # to Vapid01.from_raw(), bypassing cryptography's PEM parser (which
    # on LibreSSL macOS yields "header too long").
    priv = private_key_b64()
    body = json.dumps(payload)
    log.info("push: sending to %d subscriber(s) | %s", len(subs), payload.get("title", ""))

    def _send_one(sub_info: dict, body: str) -> tuple[int, str]:
        """Run synchronous webpush in a worker thread. Returns (status, message)."""
        try:
            resp = webpush(
                subscription_info=sub_info,
                data=body,
                vapid_private_key=priv,
                vapid_claims={"sub": VAPID_CLAIMS_EMAIL},
            )
            status = getattr(resp, "status_code", 0)
            return (status, f"OK status={status}")
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            return (status, f"WebPushException: {exc}")
        except Exception as exc:  # noqa: BLE001
            return (0, f"Exception: {type(exc).__name__}: {exc}")

    for sub in subs:
        sub_info = {
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
        }
        endpoint_short = sub["endpoint"][:70] + "..." if len(sub["endpoint"]) > 70 else sub["endpoint"]
        status, message = await _aio.to_thread(_send_one, sub_info, body)
        log.info("push -> %s : %s", endpoint_short, message)
        if status in (404, 410):
            log.info("push: subscription expired, removing from DB")
            await db.remove_push_sub(sub["endpoint"])


# ---------- Telegram send ----------

# Reasonable timeout for the Telegram Bot API: it's usually < 1s but we
# allow a generous margin so a transient slow response doesn't cancel the
# whole alert pipeline.
TELEGRAM_TIMEOUT_S = 8


def _format_telegram_message(payload: dict[str, Any]) -> str:
    """Render an alert payload as a Markdown message for Telegram.

    The same payload shape that :func:`send_push` consumes (``title`` /
    ``body`` / ``miner_id``) is used here too, so call sites don't need
    to know which channels are active.
    """
    title = str(payload.get("title", "MinerWatch"))
    body = str(payload.get("body", ""))
    # We use plain text (no parse_mode) to avoid the Telegram API
    # rejecting messages because of an accidental ``_`` or ``*`` in
    # miner names. Notifications stay readable without formatting.
    if body:
        return f"{title}\n{body}"
    return title


async def send_telegram(payload: dict[str, Any]) -> tuple[bool, str]:
    """Send a single message to the configured Telegram chat.

    Returns ``(ok, detail)``. ``ok`` is ``False`` when the channel is
    misconfigured (no token / no chat_id) OR when Telegram replied with
    an error — callers can ignore the return value if they don't care.

    This function never raises: a misconfigured or unreachable Telegram
    must not break the alert pipeline, push notifications must still
    flow to the browsers, and the alert row must still land in the DB.
    """
    cfg = get_config()
    token = (cfg.alerts.telegram_bot_token or "").strip()
    chat_id = (cfg.alerts.telegram_chat_id or "").strip()
    if not token or not chat_id:
        return (False, "missing bot token or chat_id")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    text = _format_telegram_message(payload)
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT_S) as client:
            resp = await client.post(
                url,
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "disable_web_page_preview": True,
                },
            )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code == 200 and isinstance(data, dict) and data.get("ok"):
            log.info("telegram: sent to chat %s | %s", chat_id, payload.get("title", ""))
            return (True, "ok")
        description = data.get("description") if isinstance(data, dict) else None
        msg = f"HTTP {resp.status_code}: {description or 'unknown error'}"
        log.warning("telegram: send failed (%s)", msg)
        return (False, msg)
    except httpx.TimeoutException:
        log.warning("telegram: send timed out after %ss", TELEGRAM_TIMEOUT_S)
        return (False, "timeout")
    except httpx.HTTPError as exc:
        log.warning("telegram: client error: %s", exc)
        return (False, f"network error: {exc}")
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram: unexpected error: %s", exc)
        return (False, f"unexpected: {exc}")


async def telegram_get_updates() -> dict[str, Any]:
    """Proxy Telegram's ``getUpdates`` call so the frontend can discover
    the user's ``chat_id`` without leaving the dashboard.

    Returns the raw Telegram payload (or an ``{"ok": False, "error": ...}``
    envelope if the channel is misconfigured / the network call fails).
    The Telegram getUpdates response is a list of incoming messages —
    typically the ``/start`` the user just sent to the bot — from which
    we can pull out ``message.chat.id``.
    """
    cfg = get_config()
    token = (cfg.alerts.telegram_bot_token or "").strip()
    if not token:
        return {"ok": False, "error": "missing bot token"}

    url = f"https://api.telegram.org/bot{token}/getUpdates"
    try:
        async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT_S) as client:
            resp = await client.get(url)
        try:
            return resp.json()
        except ValueError:
            return {"ok": False, "error": f"non-JSON response (HTTP {resp.status_code})"}
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram getUpdates: %s", exc)
        return {"ok": False, "error": str(exc)}


# =====================================================================
# BLOCK-FOUND FEATURE — opt-out
# =====================================================================
# Block-found = a miner produced a share whose difficulty >= the current
# Bitcoin network difficulty. For a home solo miner this is THE event:
# they just won a block. Statistically rare (years per device), but it
# happens — multiple Bitaxes have mined blocks via solo pools.
#
# How to TURN THIS OFF if it becomes a problem:
#   1) Flip the flag below to False. Nothing else needs changing.
#      Detection stops, the dispatcher in poller.py short-circuits,
#      and the home-page card simply hides (it shows only when at least
#      one find is persisted in the DB).
#   2) Past finds remain in the `block_finds` table — they're not
#      wiped. If you want a clean slate, drop the table or `DELETE
#      FROM block_finds`.
# =====================================================================

BLOCK_FOUND_ENABLED = True

# How long we cache the network difficulty value we fetched. The Bitcoin
# network difficulty only changes once every 2016 blocks (~2 weeks), so
# even 1h would be perfectly fine. The user picked 5 min for headroom.
NETWORK_DIFFICULTY_TTL_S = 300

# Public source for the fallback network difficulty. mempool.space is
# free, no API key, no rate limit relevant for our 1-call-every-5min
# usage. The endpoint returns the latest block whose `difficulty` field
# is the current network difficulty.
NETWORK_DIFFICULTY_URL = "https://mempool.space/api/v1/blocks/tip/height"
NETWORK_DIFFICULTY_BLOCK_URL = "https://mempool.space/api/v1/blocks"

# Cache slot: (value_or_None, fetched_at_ts).
_network_diff_cache: tuple[float | None, int] = (None, 0)


async def get_network_difficulty(miner_hint: float | None = None) -> float | None:
    """Return the current Bitcoin network difficulty.

    Order of preference, since we always want the most authoritative
    value with the lowest cost:

    1. ``miner_hint`` — if the miner driver (Bitaxe AxeOS) just gave us
       a fresh ``networkDifficulty`` from its stratum view, use that.
       It's free, and it's what the miner itself is using to grade its
       own shares, so the comparison is internally consistent.
    2. A cached value fetched < ``NETWORK_DIFFICULTY_TTL_S`` seconds ago.
    3. A fresh fetch from mempool.space. On failure we return whatever
       (possibly stale) cached value we have — better than nothing.
    """
    if miner_hint is not None and miner_hint > 0:
        return float(miner_hint)

    global _network_diff_cache
    cached_value, cached_ts = _network_diff_cache
    now = int(time.time())
    if cached_value is not None and (now - cached_ts) < NETWORK_DIFFICULTY_TTL_S:
        return cached_value

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            # Get tip height, then ask for its block detail, which carries
            # the current difficulty as a plain numeric field.
            height_resp = await client.get(NETWORK_DIFFICULTY_URL)
            height_resp.raise_for_status()
            tip_height = int(height_resp.text.strip())
            block_resp = await client.get(f"{NETWORK_DIFFICULTY_BLOCK_URL}/{tip_height}")
            block_resp.raise_for_status()
            blocks = block_resp.json()
            # ``/api/v1/blocks/<height>`` returns a list of 15 blocks
            # starting at the requested height (newest first).
            if isinstance(blocks, list) and blocks:
                value = float(blocks[0].get("difficulty", 0)) or None
            else:
                value = None
        if value:
            _network_diff_cache = (value, now)
            log.info("network difficulty: refreshed via mempool.space → %.2f", value)
            return value
    except Exception as exc:  # noqa: BLE001
        log.warning("network difficulty: fetch failed (%s); using cached %s", exc, cached_value)
    # If we land here, the fetch failed. Return cached value (possibly
    # None if we never managed a successful fetch yet).
    return cached_value


async def notify_block_found(
    miner_id: int,
    miner_name: str,
    share_difficulty: float,
    network_difficulty: float,
    ts: int,
) -> bool:
    """Persist a block-found event and fire a celebratory notification.

    Returns ``True`` if a new block-find row was created; ``False`` if
    suppressed by the anti-duplication guard (the same or a smaller
    share difficulty was already recorded for this miner).
    """
    # Anti-duplication: a single block-finding share should fire exactly
    # one notification + one DB row. If the firmware keeps reporting the
    # same `best_difficulty` value across polls (which it does, since
    # it's the running max), we must not re-fire.
    prev_max = await db.last_block_find_share_value(miner_id)
    if prev_max is not None and share_difficulty <= prev_max:
        return False

    row_id = await db.insert_block_find(
        miner_id=miner_id,
        miner_name=miner_name,
        share_difficulty=float(share_difficulty),
        network_difficulty=float(network_difficulty),
        ts=ts,
    )

    pretty_share = _format_si_difficulty(share_difficulty)
    pretty_network = _format_si_difficulty(network_difficulty)
    title = "🎉🎉 BLOCK FOUND!"
    body = (
        f"{miner_name} solved a Bitcoin block — "
        f"share difficulty {pretty_share} ≥ network {pretty_network}. "
        f"Congratulations!"
    )
    await db.insert_alert(miner_id, "info", "block_found", body)
    await send_notification({
        "title": title,
        "body": body,
        "miner_id": miner_id,
        "code": "block_found",
    })
    log.info("block_found: miner=%s id=%s share=%.2f network=%.2f db_row=%s",
             miner_name, miner_id, share_difficulty, network_difficulty, row_id)
    return True


# ---------- Notification dispatcher ----------

async def send_notification(payload: dict[str, Any]) -> None:
    """Fan out a single alert payload across every enabled channel.

    Honors:
      * ``alerts.notifications_enabled`` — global kill-switch, blocks
        everything. Same semantics as before this function existed.
      * ``alerts.push_enabled`` — browser push channel.
      * ``alerts.telegram_enabled`` — Telegram bot channel.

    Channels run concurrently with ``return_exceptions=True`` so a
    failure on one (Telegram down, expired subscription) cannot block
    or break the other.
    """
    cfg = get_config()
    if not cfg.alerts.notifications_enabled:
        log.info(
            "notify: globally disabled → '%s' silenced",
            payload.get("title", ""),
        )
        return

    tasks = []
    if cfg.alerts.push_enabled:
        tasks.append(send_push(payload))
    if cfg.alerts.telegram_enabled:
        tasks.append(send_telegram(payload))

    if not tasks:
        log.info(
            "notify: no channel enabled → '%s' not delivered",
            payload.get("title", ""),
        )
        return

    await asyncio.gather(*tasks, return_exceptions=True)


# ---------- Logic ----------

async def evaluate(samples: dict[int, MinerSample]) -> None:
    cfg = get_config()
    now = int(time.time())
    miners_by_id = {m["id"]: m for m in await db.list_miners()}
    repeat_s = max(60, int(cfg.alerts.repeat_seconds))

    for miner_id, sample in samples.items():
        info = miners_by_id.get(miner_id, {})
        miner_name = info.get("name") or sample.host
        prev = _state.get(miner_id, {})
        new_state: dict[str, Any] = dict(prev)

        # Online/offline transitions
        was_online = prev.get("online", True)
        new_state["online"] = sample.online

        if sample.online:
            new_state["last_online_ts"] = now
            # Symmetric with the offline branch below: we only announce a
            # "back online" if we had previously announced an "offline".
            # Without this gate, a single failed poll (e.g. AxeOS' tiny
            # web server stalling past `request_timeout`) flips the
            # in-memory state to offline, and the very next successful
            # poll fires a spurious "recovered" — even though the offline
            # window never crossed `offline_threshold_seconds` and the
            # user was never told the miner went down in the first place.
            if not was_online and prev.get("offline_alerted"):
                msg = f"{miner_name} is back online"
                await db.insert_alert(miner_id, "info", "recovered", msg)
                await send_notification({"title": "Miner online", "body": msg, "miner_id": miner_id})
        else:
            last_online = prev.get("last_online_ts", now)
            offline_for = now - last_online
            last_offline_alert = prev.get("last_offline_alert_ts", 0)
            if offline_for >= cfg.alerts.offline_threshold_seconds and (
                not prev.get("offline_alerted") or (now - last_offline_alert) >= repeat_s
            ):
                msg = f"{miner_name} has not responded for {offline_for}s"
                await db.insert_alert(miner_id, "warning", "offline", msg)
                await send_notification({"title": "Miner offline", "body": msg, "miner_id": miner_id})
                new_state["offline_alerted"] = True
                new_state["last_offline_alert_ts"] = now

            _state[miner_id] = new_state
            continue

        # Reset offline flag once it is back online
        new_state.pop("offline_alerted", None)
        new_state.pop("last_offline_alert_ts", None)

        # ----- Chip temperature threshold -----
        chip_threshold = info.get("fan_threshold_c") or cfg.alerts.temp_chip_threshold
        if sample.temp_chip_c is not None and sample.temp_chip_c >= chip_threshold:
            last_chip_alert = prev.get("last_chip_alert_ts", 0)
            should_alert = (
                not prev.get("chip_alerted")
                or (now - last_chip_alert) >= repeat_s
            )
            if should_alert:
                msg = (
                    f"{miner_name}: chip temperature {sample.temp_chip_c:.1f}°C "
                    f"(threshold {chip_threshold}°C)"
                )
                await db.insert_alert(miner_id, "critical", "temp_chip", msg)
                await send_notification({"title": "High chip temp", "body": msg, "miner_id": miner_id})
                new_state["chip_alerted"] = True
                new_state["last_chip_alert_ts"] = now
        else:
            new_state.pop("chip_alerted", None)
            new_state.pop("last_chip_alert_ts", None)

        # ----- VR temperature threshold -----
        if sample.temp_vr_c is not None and sample.temp_vr_c >= cfg.alerts.temp_vr_threshold:
            last_vr_alert = prev.get("last_vr_alert_ts", 0)
            should_alert = (
                not prev.get("vr_alerted")
                or (now - last_vr_alert) >= repeat_s
            )
            if should_alert:
                msg = (
                    f"{miner_name}: VR temperature {sample.temp_vr_c:.1f}°C "
                    f"(threshold {cfg.alerts.temp_vr_threshold}°C)"
                )
                await db.insert_alert(miner_id, "critical", "temp_vr", msg)
                await send_notification({"title": "High VR temp", "body": msg, "miner_id": miner_id})
                new_state["vr_alerted"] = True
                new_state["last_vr_alert_ts"] = now
        else:
            new_state.pop("vr_alerted", None)
            new_state.pop("last_vr_alert_ts", None)

        _state[miner_id] = new_state


# ---------- Best-share notifications ----------

def _format_si_difficulty(value: float, decimals: int = 2) -> str:
    """Compact SI representation of a raw difficulty number.

    Mirrors the frontend ``fmtDifficulty`` so the push body shows the
    same format users see in the UI (e.g. ``4.29 G``).
    """
    if value is None:
        return "—"
    n = float(value)
    if n == 0:
        return "0"
    units = (
        (1e24, "Y"),
        (1e21, "Z"),
        (1e18, "E"),
        (1e15, "P"),
        (1e12, "T"),
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
    )
    abs_n = abs(n)
    for v, s in units:
        if abs_n >= v:
            return f"{n / v:.{decimals}f} {s}"
    return f"{n:.0f}"


async def notify_new_alltime_best(
    miner_id: int,
    miner_name: str,
    prev_value: float | None,
    new_value: float,
    ts: int,
) -> bool:
    """Maybe send a "new best share" push for a freshly-broken all-time record.

    Returns ``True`` if a notification was actually emitted, ``False`` if
    it was suppressed by one of the anti-spam guards.

    Suppression rules (all evaluated; first that matches wins):
      1. ``prev_value is None`` — first record ever for this miner; we
         silently seed it without notifying.
      2. ``new_value < prev_value * BEST_SHARE_MIN_GROWTH`` — the bump
         is too small to be interesting (< +10% by default).
      3. The miner emitted a best-share push in the last
         ``BEST_SHARE_COOLDOWN_S`` seconds.
    """
    state = _state.setdefault(miner_id, {})

    if prev_value is None:
        # Seed: remember we've seen one record so future bumps qualify.
        state["best_share_alltime_seeded_ts"] = ts
        return False

    if new_value < float(prev_value) * BEST_SHARE_MIN_GROWTH:
        return False

    last_push = state.get("best_share_alltime_last_push_ts", 0)
    if ts - last_push < BEST_SHARE_COOLDOWN_S:
        return False

    pretty_new = _format_si_difficulty(new_value)
    pretty_prev = _format_si_difficulty(prev_value)
    title = "🎯 New best share"
    body = f"{miner_name}: {pretty_new} (was {pretty_prev})"
    await db.insert_alert(miner_id, "info", "best_share_alltime", body)
    await send_notification({
        "title": title,
        "body": body,
        "miner_id": miner_id,
        "code": "best_share_alltime",
    })

    state["best_share_alltime_last_push_ts"] = ts
    return True

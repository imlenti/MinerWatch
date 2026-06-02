# SPDX-License-Identifier: AGPL-3.0-only
"""Bitcoin spot price (USD) for the ESPHome panel's price screen.

The ESP32 panels can't talk HTTPS comfortably (the Cheap-Yellow-Display has
no PSRAM and is already tight on RAM with LVGL), so MinerWatch fetches the
price here and ships it to the panel over the MQTT feed it already consumes.
We reuse mempool.space — the very same upstream the network-difficulty
fallback in ``alerts.py`` uses — and cache briefly so we never hammer it.

Failures are soft: on any error we return the last known value (possibly
``None``) so a transient API hiccup just shows a slightly stale price rather
than blanking the panel. Mirrors the ``coin_difficulty`` cache pattern.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

import httpx

log = logging.getLogger("minerwatch.btc_price")

# mempool.space spot prices — free, no API key, no rate limit relevant for
# our once-a-minute usage. Same host as the difficulty fallback. The endpoint
# returns: {"time": <epoch>, "USD": 67432, "EUR": ..., "GBP": ..., ...}.
PRICES_URL = "https://mempool.space/api/v1/prices"

# The panel only needs a "good enough" spot value; 60s keeps it fresh while
# staying gentle on the API (mempool refreshes its own price about that often).
_CACHE_TTL_S = 60

# Cache slot: (value_or_None, fetched_at_epoch). Module-level, like the
# difficulty cache — a single process-wide spot price is all we need.
_cache: Tuple[Optional[float], int] = (None, 0)


async def get_btc_usd() -> Optional[float]:
    """Return the latest BTC price in USD.

    Order of preference:
      1. A cached value fetched < ``_CACHE_TTL_S`` seconds ago.
      2. A fresh fetch from mempool.space.
      3. On failure, whatever (possibly stale) cached value we have — better
         than nothing; ``None`` only if we never managed a successful fetch.
    """
    global _cache
    cached_value, cached_ts = _cache
    now = int(time.time())
    if cached_value is not None and (now - cached_ts) < _CACHE_TTL_S:
        return cached_value

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(PRICES_URL, headers={"User-Agent": "MinerWatch"})
            resp.raise_for_status()
            payload = resp.json()
        raw = payload.get("USD") if isinstance(payload, dict) else None
        value = float(raw) if raw is not None else None
        if value and value > 0:
            _cache = (value, now)
            log.info("btc price: refreshed via mempool.space -> $%.0f", value)
            return value
        log.warning("btc price: response missing a usable USD field: %r", payload)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("btc price: fetch failed (%s); using cached %s", exc, cached_value)
    return cached_value


def last_fetched_ts() -> int:
    """Epoch of the last successful fetch (0 if we never fetched one)."""
    return _cache[1]


def _reset_cache_for_tests() -> None:
    """Clear the module cache (used by unit tests)."""
    global _cache
    _cache = (None, 0)

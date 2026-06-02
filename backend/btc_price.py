# SPDX-License-Identifier: AGPL-3.0-only
"""Bitcoin spot price + 24h change (USD) for the ESPHome panel price screen.

The ESP32 panels can't talk HTTPS comfortably (the Cheap-Yellow-Display has no
PSRAM and is already tight on RAM with LVGL), so MinerWatch fetches everything
here and ships it to the panel over the MQTT feed it already consumes. We use
mempool.space — the same upstream the network-difficulty fallback in
``alerts.py`` uses:

  * ``/api/v1/prices``           → the current USD spot price.
  * ``/api/v1/historical-price`` → the USD price ~24h ago, from which we derive
                                   the signed 24h change %.

Cached ~60s, soft-fail: on any error we return the last known values (the 24h
change is best-effort — if only the historical call fails we still return the
spot price and the panel simply hides the change). Mirrors the
``coin_difficulty`` cache pattern.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Tuple

import httpx

log = logging.getLogger("minerwatch.btc_price")

# mempool.space — free, no API key, no rate limit relevant for once-a-minute use.
PRICES_URL = "https://mempool.space/api/v1/prices"
HISTORICAL_URL = "https://mempool.space/api/v1/historical-price"
_UA = {"User-Agent": "MinerWatch"}

# A "good enough" spot value; 60s keeps it fresh while staying gentle on the API.
_CACHE_TTL_S = 60
_DAY_S = 24 * 60 * 60

# Process-wide cache: usd, signed 24h change %, and the fetch epoch.
_cache: dict = {"usd": None, "chg": None, "ts": 0}


async def _fetch(now: int) -> Tuple[Optional[float], Optional[float]]:
    """Fetch the spot price and (best-effort) the 24h change.

    Returns ``(usd, change_pct_or_None)``. ``usd`` is ``None`` when the price
    response had nothing usable. The historical call is wrapped separately so a
    failure there never costs us the spot price.
    """
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.get(PRICES_URL, headers=_UA)
        resp.raise_for_status()
        body = resp.json() if resp.content else {}
        usd = float(body["USD"]) if isinstance(body, dict) and body.get("USD") else None
        if not usd or usd <= 0:
            return None, None

        chg: Optional[float] = None
        try:
            h = await client.get(
                HISTORICAL_URL,
                params={"currency": "USD", "timestamp": now - _DAY_S},
                headers=_UA,
            )
            h.raise_for_status()
            hb = h.json() if h.content else {}
            prices = hb.get("prices") if isinstance(hb, dict) else None
            if prices:
                past = float(prices[0]["USD"]) if prices[0].get("USD") else None
                if past and past > 0:
                    chg = (usd - past) / past * 100.0
        except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError) as exc:
            log.warning("btc price: 24h change fetch failed (%s)", exc)
        return usd, chg


async def get_btc() -> Tuple[Optional[float], Optional[float], int]:
    """Return ``(usd, change_pct_or_None, fetched_at_epoch)``.

    Cached for ``_CACHE_TTL_S``; on a refresh failure returns the last known
    values (``usd`` is ``None`` only if we never fetched successfully yet).
    """
    now = int(time.time())
    if _cache["usd"] is not None and (now - _cache["ts"]) < _CACHE_TTL_S:
        return _cache["usd"], _cache["chg"], _cache["ts"]

    try:
        usd, chg = await _fetch(now)
        if usd:
            _cache.update(usd=usd, chg=chg, ts=now)
            log.info(
                "btc price: refreshed via mempool.space -> $%.0f (%s24h)",
                usd, f"{chg:+.2f}% " if chg is not None else "no ",
            )
            return usd, chg, now
        log.warning("btc price: response had no usable USD value")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        log.warning("btc price: fetch failed (%s); using cached %s", exc, _cache["usd"])
    return _cache["usd"], _cache["chg"], _cache["ts"]


def _reset_cache_for_tests() -> None:
    """Clear the module cache (used by unit tests)."""
    _cache.update(usd=None, chg=None, ts=0)

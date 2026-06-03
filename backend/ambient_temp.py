# SPDX-License-Identifier: AGPL-3.0-only
"""Ambient temperature relay for the ESP32 panel.

Some setups have a separate sensor (e.g. an Arduino/Grove probe) publishing a
plain-text Celsius value to its own MQTT topic on the same broker MinerWatch
uses. Rather than wiring that into every panel, MinerWatch can subscribe to it,
keep a small rolling state, and ship the result inside the panel feed.

State kept here (mirrors a field-tested receiver):
  * current  — mean of the values seen in the last ``WINDOW_S`` seconds;
  * min/max  — extremes of the *real* values received this session (not the
               average), so a brief spike still registers;
  * available — true only if a valid value arrived within ``AVAIL_S`` seconds.
               When false the panel shows "-" for the current value but keeps
               min/max. Availability is based on data *freshness* only, NOT on
               the status topic: a sensor that's truly offline stops sending, so
               the reading goes stale on its own — while a stale *retained*
               "offline" (a common Arduino LWT delivered the moment we
               subscribe) must never hide a live reading.

No I/O here — the MQTT client feeds ``update()`` / ``set_status()`` and reads
``snapshot()`` when publishing. Pure and unit-testable.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

WINDOW_S = 60.0          # moving-average window for the current value
AVAIL_S = 20.0           # data older than this -> "unavailable"
VALID_MIN_C = -50.0      # sanity bounds; values outside are ignored
VALID_MAX_C = 125.0


@dataclass
class AmbientSnapshot:
    current_c: float | None      # None when unavailable (stale / offline)
    min_c: float | None
    max_c: float | None
    available: bool
    has_data: bool               # have we ever received a valid value?


class AmbientTemp:
    def __init__(self) -> None:
        self._samples: deque[tuple[float, float]] = deque()  # (monotonic_ts, value)
        self._min: float | None = None
        self._max: float | None = None
        self._last_seen: float = 0.0
        self._offline: bool = False

    def update(self, value: float) -> bool:
        """Record a temperature reading. Returns False if it was rejected."""
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if not (VALID_MIN_C <= v <= VALID_MAX_C):
            return False
        now = time.monotonic()
        self._samples.append((now, v))
        self._last_seen = now
        self._min = v if self._min is None else min(self._min, v)
        self._max = v if self._max is None else max(self._max, v)
        return True

    def set_status(self, status: str) -> None:
        """Record the optional publisher status (online/offline).

        Kept for reference/diagnostics only — availability is decided by data
        freshness (see ``snapshot``), so this never suppresses a live reading.
        """
        self._offline = str(status).strip().lower() == "offline"

    def _prune(self, now: float) -> None:
        cutoff = now - WINDOW_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    def snapshot(self) -> AmbientSnapshot:
        now = time.monotonic()
        self._prune(now)
        # Freshness alone decides availability (see class docstring): a stale
        # retained "offline" on the status topic must not hide live data.
        available = self._last_seen > 0 and (now - self._last_seen) <= AVAIL_S
        if available and self._samples:
            current = sum(v for _, v in self._samples) / len(self._samples)
        else:
            current = None
        return AmbientSnapshot(
            current_c=current,
            min_c=self._min,
            max_c=self._max,
            available=available,
            has_data=self._min is not None,
        )

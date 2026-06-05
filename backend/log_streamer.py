# SPDX-License-Identifier: AGPL-3.0-only
"""Live per-share streamer for AxeOS miners (Bitaxe / NerdQAxe / Titan).

The REST poller in :mod:`backend.poller` only sees *aggregates*: the
firmware's running ``bestDiff``, the share counters, a smoothed
hashrate. It can never show the individual shares as they happen.

AxeOS exposes its runtime system log over a WebSocket at
``ws://<ip>/api/ws``. Every nonce the ASIC returns is logged as an
``asic_result`` line that carries the share difficulty and the current
pool target, e.g. (ANSI colour codes stripped)::

    I (446925478) asic_result: ID: 69fd…, ASIC nr: 0, ver: 22816000 \
        Nonce 639505B4 diff 3035.4 of 1497.

``diff 3035.4`` is the difficulty of *this* result; ``of 1497.`` is the
pool/stratum target in force at that moment (ckpool vardiff). A result
is *submitted* to the pool when ``diff >= target``; the firmware then
logs a ``stratum_api: tx`` line and, a few ms later, a
``stratum_task: message result accepted`` (or ``rejected``).

This module opens one persistent WebSocket per enabled AxeOS miner,
parses that stream, and:

* keeps the last ``RING_BUFFER`` share events in memory per miner
  (for the live chart),
* fans every new event out to any number of SSE subscribers,
* persists *notable* shares (>= :data:`NOTABLE_THRESHOLD`) to the
  ``notable_shares`` table so the "near-block Hall of Fame" survives a
  restart.

Privacy: the ``stratum_api: tx`` lines contain the user's payout
address and worker name. We parse-and-discard — only the numeric
difficulty/target ever leave this module. Raw stratum lines are never
buffered or written to disk.

Scope: AxeOS only. cgminer-family miners (Canaan/Braiins/LuxOS) expose
a JSON API on :4028 with no equivalent per-share log stream, so they
are simply skipped by the reconcile loop.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

from . import db

log = logging.getLogger("minerwatch.log_streamer")

# `websockets` ships transitively with uvicorn[standard]; import it
# defensively so a stripped-down environment degrades to "feature off"
# instead of crashing the whole app at import time.
try:
    import websockets  # type: ignore
    from websockets.exceptions import WebSocketException  # type: ignore

    _WEBSOCKETS_AVAILABLE = True
except Exception:  # noqa: BLE001  pragma: no cover
    websockets = None  # type: ignore
    WebSocketException = Exception  # type: ignore
    _WEBSOCKETS_AVAILABLE = False


# Families that speak the AxeOS REST/WS protocol. `bitaxe` covers both
# the original Bitaxe and the NerdQAxe (they share the family tag in the
# DB); `nerdoctaxe` is the multi-ASIC fork, also AxeOS-derived.
AXEOS_FAMILIES = {"bitaxe", "nerdoctaxe"}

# Per-miner ring buffer for the live chart. Retention is primarily
# TIME-based (RING_BUFFER_SECONDS): a pure count cap spans far less
# wall-clock time for a high-throughput multi-ASIC board (e.g. the
# SupraHex, which logs results several times faster than a single-ASIC
# Gamma) than for a slow one, so a fast miner's older shares fell out of
# the snapshot and "dropped off" the left of the chart while slow miners
# still scrolled the full window. RING_BUFFER is the count backstop
# (a hard memory bound) layered on top of the time window.
RING_BUFFER = 20000
RING_BUFFER_SECONDS = 15 * 60  # ≥ the longest frontend chart range (10m) + margin

# A share is "notable" — worth persisting to the Hall of Fame — when its
# difficulty clears this floor. On a solo pool the vardiff target is
# tiny (~1.5k), so almost every submitted share would qualify; the floor
# keeps the table to genuine near-misses. Tunable via the settings DB
# key ``streaming.notable_threshold`` if a user wants it lower/higher.
NOTABLE_THRESHOLD = 1_000_000.0

# Keep at most this many Hall-of-Fame rows per miner (top-by-difficulty).
NOTABLE_KEEP_PER_MINER = 500

# Reconcile the set of streaming miners against the DB at this cadence.
RECONCILE_INTERVAL_S = 15

# Per-subscriber queue bound. A slow SSE client (e.g. a backgrounded
# phone) must never make the producer grow without limit: once the queue
# is full we drop the oldest event for that subscriber only.
SUBSCRIBER_QUEUE_MAX = 1000

# ---- parsing -------------------------------------------------------------
# Lines arrive wrapped in ESP-IDF ANSI colour codes, e.g.
# "\x1b[0;32mI (123) tag: msg\x1b[0m". Strip those first.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# "<LEVEL> (<ms_since_boot>) <tag>: <message>"
_LOG = re.compile(r"^([IWE]) \((\d+)\) ([^:]+): (.*)$")
# Share-line difficulty, two firmware dialects:
#   AxeOS / Bitaxe:  "... diff 3035.4 of 1497."   → "<share> of <target>"
#   NerdQAxe(++):    "... diff 1394.8/3065/241M"  → "<share>/<target>/<best>"
# We capture the share difficulty + the pool target from either. The
# optional SI suffix (k/M/G/T/…) is parsed too, in case a pool runs a
# high vardiff that the firmware prints in SI form.
_SHARE = re.compile(r"\bdiff\s+([0-9.]+[kKMGTPE]?)\s*(?:of|/)\s*([0-9.]+[kKMGTPE]?)")

_SI_SUFFIX = {"k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}


def _parse_diff_token(tok: str) -> float:
    """Parse a difficulty token that may carry an SI suffix ("4.29G")."""
    tok = tok.strip()
    if tok and tok[-1] in _SI_SUFFIX:
        return float(tok[:-1]) * _SI_SUFFIX[tok[-1]]
    return float(tok)


@dataclass
class ShareEvent:
    """One ASIC result parsed off the log stream.

    ``ts`` is the wall-clock arrival time on the MinerWatch host, NOT the
    miner's log timestamp: the firmware logs milliseconds-since-boot,
    which resets on reboot and is useless for charting. ``uptime_ms`` is
    kept only for ordering/debugging.
    """

    seq: int
    miner_id: int
    ts: float
    uptime_ms: int
    share_diff: float
    pool_target: float
    submitted: bool
    accepted: Optional[bool] = None
    # rowid of the persisted Hall-of-Fame row, if this share was notable.
    # Lets us back-fill `accepted` once the stratum result line arrives.
    _notable_rowid: Optional[int] = None

    def to_public(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "diff": self.share_diff,
            "target": self.pool_target,
            "submitted": self.submitted,
            "accepted": self.accepted,
        }


@dataclass
class MinerStream:
    """Per-miner streaming state."""

    miner_id: int
    host: str
    port: int
    buffer: Deque[ShareEvent] = field(default_factory=lambda: deque(maxlen=RING_BUFFER))
    # Submitted shares awaiting their accepted/rejected verdict, oldest
    # first. Bounded so an unmatched result line can't leak memory.
    pending: Deque[ShareEvent] = field(default_factory=lambda: deque(maxlen=64))
    seq: int = 0
    current_target: Optional[float] = None
    results_total: int = 0
    submitted_total: int = 0
    accepted_total: int = 0
    rejected_total: int = 0
    connected: bool = False
    last_event_ts: Optional[float] = None
    started_at: float = field(default_factory=time.time)

    def stats(self) -> dict[str, Any]:
        return {
            "miner_id": self.miner_id,
            "connected": self.connected,
            "current_target": self.current_target,
            "results_total": self.results_total,
            "submitted_total": self.submitted_total,
            "accepted_total": self.accepted_total,
            "rejected_total": self.rejected_total,
            "last_event_ts": self.last_event_ts,
            "buffered": len(self.buffer),
            "since": self.started_at,
        }


class LogStreamer:
    """Manages one WS task per AxeOS miner, plus the SSE pub/sub."""

    def __init__(self) -> None:
        self._streams: dict[int, MinerStream] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._subscribers: dict[int, set[asyncio.Queue]] = {}
        self._manager: asyncio.Task | None = None
        self._stop = asyncio.Event()

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        if not _WEBSOCKETS_AVAILABLE:
            log.warning(
                "websockets library unavailable — live per-share streaming "
                "disabled. `pip install websockets` to enable it."
            )
            return
        if self._manager and not self._manager.done():
            return
        self._stop.clear()
        self._manager = asyncio.create_task(self._reconcile_loop(), name="mw-log-streamer")
        log.info("Log streamer started")

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._manager:
            self._manager.cancel()
            try:
                await self._manager
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._manager = None
        log.info("Log streamer stopped")

    # ---- reconcile ------------------------------------------------------

    async def _reconcile_loop(self) -> None:
        """Keep the running WS tasks in sync with the enabled AxeOS miners."""
        while not self._stop.is_set():
            try:
                await self._reconcile_once()
            except Exception:  # noqa: BLE001
                log.exception("log-streamer reconcile error")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=RECONCILE_INTERVAL_S)
            except asyncio.TimeoutError:
                continue

    async def _reconcile_once(self) -> None:
        miners = await db.list_miners(only_enabled=True)
        wanted: dict[int, tuple[str, int]] = {}
        for m in miners:
            if (m.get("family") or "").lower() not in AXEOS_FAMILIES:
                continue
            mid = int(m["id"])
            host = m.get("host") or ""
            port = int(m.get("port") or 80)
            if host:
                wanted[mid] = (host, port)

        # Stop tasks for miners that vanished, got disabled, or moved host.
        for mid in list(self._tasks.keys()):
            task = self._tasks[mid]
            stream = self._streams.get(mid)
            moved = stream is not None and mid in wanted and (stream.host, stream.port) != wanted[mid]
            if mid not in wanted or task.done() or moved:
                task.cancel()
                self._tasks.pop(mid, None)
                self._streams.pop(mid, None)

        # Start tasks for newly-wanted miners.
        for mid, (host, port) in wanted.items():
            if mid in self._tasks and not self._tasks[mid].done():
                continue
            stream = MinerStream(miner_id=mid, host=host, port=port)
            self._streams[mid] = stream
            self._tasks[mid] = asyncio.create_task(
                self._run_miner(stream), name=f"mw-stream-{mid}"
            )

    # ---- per-miner WS task ---------------------------------------------

    def _ws_url(self, host: str, port: int) -> str:
        # AxeOS serves the log WS on the HTTP port (80). Only include an
        # explicit port when it's non-standard.
        if port and port != 80:
            return f"ws://{host}:{port}/api/ws"
        return f"ws://{host}/api/ws"

    async def _run_miner(self, stream: MinerStream) -> None:
        url = self._ws_url(stream.host, stream.port)
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(
                    url,
                    origin=f"http://{stream.host}",  # AxeOS validates Origin
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=8,
                    close_timeout=4,
                    max_size=None,
                    max_queue=64,
                ) as ws:
                    stream.connected = True
                    backoff = 1.0
                    log.info("streaming %s (miner %s)", url, stream.miner_id)
                    async for message in ws:
                        if self._stop.is_set():
                            break
                        text = message if isinstance(message, str) else message.decode("utf-8", "replace")
                        # A frame can technically carry more than one line.
                        for line in text.splitlines():
                            await self._handle_line(stream, line)
            except asyncio.CancelledError:
                break
            except (WebSocketException, OSError, asyncio.TimeoutError) as exc:
                log.debug("stream %s dropped: %s", url, exc)
            except Exception:  # noqa: BLE001
                log.exception("unexpected stream error for %s", url)
            finally:
                stream.connected = False

            if self._stop.is_set():
                break
            # Reconnect with capped exponential backoff.
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)

    # ---- parsing + dispatch --------------------------------------------

    async def _handle_line(self, stream: MinerStream, line: str) -> None:
        clean = _ANSI.sub("", line).strip()
        if not clean:
            return
        m = _LOG.match(clean)
        if not m:
            return
        _level, ms_str, tag, msg = m.groups()
        tag = tag.strip()

        if tag == "asic_result":
            sm = _SHARE.search(msg)
            if not sm:
                return
            try:
                share_diff = _parse_diff_token(sm.group(1))
                pool_target = _parse_diff_token(sm.group(2))
            except ValueError:
                return
            try:
                uptime_ms = int(ms_str)
            except ValueError:
                uptime_ms = 0
            await self._on_result(stream, uptime_ms, share_diff, pool_target)
        # Bitaxe logs the verdict under tag "stratum_task"; NerdQAxe uses
        # "stratum task (Pri)" (space + pool marker). Match both dialects.
        elif tag.startswith("stratum_task") or tag.startswith("stratum task"):
            if "result accepted" in msg:
                self._on_verdict(stream, accepted=True)
            elif "result rejected" in msg:
                self._on_verdict(stream, accepted=False)

    async def _on_result(
        self, stream: MinerStream, uptime_ms: int, share_diff: float, pool_target: float
    ) -> None:
        stream.seq += 1
        stream.results_total += 1
        stream.current_target = pool_target
        now = time.time()
        stream.last_event_ts = now
        submitted = share_diff >= pool_target
        ev = ShareEvent(
            seq=stream.seq,
            miner_id=stream.miner_id,
            ts=now,
            uptime_ms=uptime_ms,
            share_diff=share_diff,
            pool_target=pool_target,
            submitted=submitted,
        )
        stream.buffer.append(ev)
        # Time-based retention on top of the deque's count cap: drop events
        # older than the chart window so a fast miner keeps the same time
        # span as a slow one. Expired events are always at the front (ts
        # ascending), so this is amortised O(1) per result.
        cutoff = now - RING_BUFFER_SECONDS
        buf = stream.buffer
        while buf and buf[0].ts < cutoff:
            buf.popleft()
        if submitted:
            stream.submitted_total += 1
            stream.pending.append(ev)

        # Persist near-block-class shares so the Hall of Fame survives a
        # restart. Always submitted (diff >> target), so a verdict line
        # will follow and back-fill `accepted`.
        if share_diff >= NOTABLE_THRESHOLD:
            try:
                rowid = await db.insert_notable_share(
                    miner_id=stream.miner_id,
                    ts=int(now),
                    share_difficulty=share_diff,
                    pool_target=pool_target,
                    keep_per_miner=NOTABLE_KEEP_PER_MINER,
                )
                ev._notable_rowid = rowid
            except Exception:  # noqa: BLE001
                log.exception("failed to persist notable share for %s", stream.miner_id)

        self._publish(stream.miner_id, {"type": "share", "data": ev.to_public()})

    def _on_verdict(self, stream: MinerStream, accepted: bool) -> None:
        if accepted:
            stream.accepted_total += 1
        else:
            stream.rejected_total += 1
        if not stream.pending:
            return
        ev = stream.pending.popleft()
        ev.accepted = accepted
        # Back-fill the persisted Hall-of-Fame row's verdict, if any.
        if ev._notable_rowid is not None:
            asyncio.create_task(
                self._update_notable_accepted(ev._notable_rowid, accepted)
            )
        self._publish(
            stream.miner_id,
            {"type": "verdict", "data": {"seq": ev.seq, "accepted": accepted}},
        )

    async def _update_notable_accepted(self, rowid: int, accepted: bool) -> None:
        try:
            await db.set_notable_share_accepted(rowid, accepted)
        except Exception:  # noqa: BLE001
            log.debug("could not update notable share %s verdict", rowid)

    # ---- pub/sub --------------------------------------------------------

    def _publish(self, miner_id: int, event: dict[str, Any]) -> None:
        subs = self._subscribers.get(miner_id)
        if not subs:
            return
        for q in list(subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop its oldest event to make room.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass

    def subscribe(self, miner_id: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        self._subscribers.setdefault(miner_id, set()).add(q)
        return q

    def unsubscribe(self, miner_id: int, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(miner_id)
        if subs:
            subs.discard(q)
            if not subs:
                self._subscribers.pop(miner_id, None)

    # ---- read helpers (used by the API) --------------------------------

    def is_supported(self, family: Optional[str]) -> bool:
        return (family or "").lower() in AXEOS_FAMILIES

    def recent(self, miner_id: int, limit: int = RING_BUFFER) -> list[dict[str, Any]]:
        stream = self._streams.get(miner_id)
        if not stream:
            return []
        events = list(stream.buffer)
        if limit and limit < len(events):
            events = events[-limit:]
        return [e.to_public() for e in events]

    def stats(self, miner_id: int) -> Optional[dict[str, Any]]:
        stream = self._streams.get(miner_id)
        return stream.stats() if stream else None


# Global instance (mirrors backend.poller.poller).
log_streamer = LogStreamer()

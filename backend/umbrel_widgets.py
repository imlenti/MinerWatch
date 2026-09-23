# SPDX-License-Identifier: AGPL-3.0-only
"""umbrelOS desktop-widget payload builders.

umbrelOS 1.x lets an app declare desktop widgets in its manifest
(``umbrel-app.yml`` → ``widgets:``): umbreld periodically fetches an
HTTP endpoint inside the app and renders the JSON it returns. This
module builds those payloads for MinerWatch's two widgets:

  * ``fleet``  — type ``four-stats``: hashrate / miners online / best
    share / max chip temp. For :data:`BLOCK_CELEBRATION_SECONDS` after
    a block find the four cells switch to a celebration layout
    (height, finder, share difficulty, date) — the widget type itself
    cannot change at runtime (umbrelOS picks the React component from
    the *manifest* type), so the celebration reuses the same 2×2 grid
    with different content.

  * ``miners`` — type ``list``: one row per enabled miner, name+model
    on the dim label line, hashrate+temp on the value line. umbrelOS
    renders at most 5 rows, so with larger fleets we emit 4 real rows
    plus an aggregate "+N more miners" row.

Everything here is a pure function of plain inputs (miner rows, live
samples, records, ``now``) so it unit-tests without FastAPI or the DB.
All rendered values are strings — umbrelOS displays them verbatim, and
the official widgets (e.g. Public Pool) follow the same convention.

Formatting mirrors ``frontend-react/src/lib/format.ts`` (SI suffixes
for difficulty, TH/s vs GH/s cutoff) so a number on the Umbrel desktop
matches the same number in the dashboard.

Plumbing note: umbreld resolves the manifest ``endpoint`` hostname as a
compose *service name* and fetches its bridge-network IP. MinerWatch's
``web`` service is host-networked (no bridge IP), so the manifest points
at ``widgets:8765``, a small relay service in the Umbrel compose that
forwards to the host port. (It used to be ``app_proxy:8000``, but
umbrelOS 2.0 no longer runs app_proxy as a container.) The two
endpoints are auth-exempt (see ``auth.public_paths``) because umbreld
fetches without a session cookie; they expose only coarse fleet numbers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

# How long the fleet widget stays in celebration mode after a block
# find. A block is a once-in-a-lifetime event for a small solo fleet;
# a week of confetti feels right without retiring the widget's day job.
BLOCK_CELEBRATION_SECONDS = 7 * 24 * 3600

# umbrelOS's list widget renders items.slice(0, 5).
LIST_MAX_ROWS = 5

# Poll interval for both widgets. umbreld needs ``refresh`` inside the
# *data response*: widgets/routes.ts runs ``ms(widgetData.refresh)`` on
# whatever the endpoint returns (the manifest ``refresh`` is never merged
# in), and ``ms(undefined)`` throws — leaving the widget stuck on its
# skeleton. Keep in sync with the manifest in umbrel/umbrel-app.yml.
WIDGET_REFRESH = "5s"

# SI suffixes, largest first — mirrors SI_UNITS in lib/format.ts.
_SI_UNITS = (
    (1e24, "Y"),
    (1e21, "Z"),
    (1e18, "E"),
    (1e15, "P"),
    (1e12, "T"),
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
)

_DASH = "—"


def _fmt_si(value: float | None, decimals: int = 2) -> tuple[str, str]:
    """Split a difficulty-like number into (mantissa, SI suffix).

    4_290_000_000 → ("4.29", "G"); 742 → ("742", ""); None → ("—", "").
    Kept as a tuple because four-stats renders value and unit with
    different opacities (text vs subtext).
    """
    if value is None:
        return (_DASH, "")
    n = float(value)
    if n == 0:
        return ("0", "")
    for unit_value, suffix in _SI_UNITS:
        if abs(n) >= unit_value:
            return (f"{n / unit_value:.{decimals}f}", suffix)
    return (f"{n:.0f}", "")


def _fmt_hashrate(ths: float | None) -> tuple[str, str]:
    """(value, unit) for a hashrate in TH/s: ("4.78", "TH/s") / ("850", "GH/s")."""
    if ths is None:
        return (_DASH, "")
    if ths >= 1.0:
        return (f"{ths:.2f}", "TH/s")
    return (f"{ths * 1000:.0f}", "GH/s")


def _fmt_hashrate_inline(ths: float | None) -> str | None:
    """Single-string hashrate for list rows ("1.21 TH/s"), None when unknown."""
    if ths is None:
        return None
    value, unit = _fmt_hashrate(ths)
    return f"{value} {unit}"


def _fmt_duration(seconds: float) -> str:
    """Coarse human duration: "12 m", "5 h", "3 d". Floors at 1 m."""
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{max(1, seconds // 60)} m"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def _fmt_date(ts: float) -> str:
    """"Jun 8" (UTC). Day-level granularity, so the timezone shift is moot."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return f"{dt.strftime('%b')} {dt.day}"


def _nominal_ths(sample: Any) -> float:
    """Sort key for list rows: the firmware's expected hashrate when
    available (stable across poll jitter — that's the point: rows must
    not reshuffle on every 5 s refresh), else the smoothed live value."""
    for attr in ("expected_hashrate_ths", "hashrate_ths"):
        v = getattr(sample, attr, None)
        if v:
            return float(v)
    return 0.0


def build_fleet_widget(
    miners: list[Mapping[str, Any]],
    samples: Mapping[int, Any],
    best_alltime: float | None,
    latest_find: Mapping[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    """The ``four-stats`` payload: normal stats or block-find celebration.

    ``miners`` are the *enabled* miner rows; ``samples`` is the poller's
    live snapshot map keyed by miner id; ``best_alltime`` is the fleet
    all-time best-share value; ``latest_find`` is the newest
    ``block_finds`` row (or None).
    """
    if latest_find is not None:
        age = now - float(latest_find.get("ts") or 0)
        if 0 <= age <= BLOCK_CELEBRATION_SECONDS:
            return _fleet_celebration(latest_find)

    online = 0
    total_ths = 0.0
    max_temp: float | None = None
    for m in miners:
        sample = samples.get(m["id"])
        if not (sample and getattr(sample, "online", False)):
            continue
        online += 1
        if sample.hashrate_ths is not None:
            total_ths += float(sample.hashrate_ths)
        temp = getattr(sample, "temp_chip_c", None)
        if temp is not None and (max_temp is None or temp > max_temp):
            max_temp = float(temp)

    hr_text, hr_unit = _fmt_hashrate(total_ths if online else None)
    best_text, best_suffix = _fmt_si(best_alltime)

    return {
        "type": "four-stats",
        "refresh": WIDGET_REFRESH,
        "link": "",
        "items": [
            {"title": "Hashrate", "text": hr_text, "subtext": hr_unit},
            {"title": "Miners online", "text": f"{online}/{len(miners)}", "subtext": ""},
            {"title": "Best share", "text": best_text, "subtext": best_suffix},
            {
                "title": "Max temp",
                "text": f"{round(max_temp)}" if max_temp is not None else _DASH,
                "subtext": "°C" if max_temp is not None else "",
            },
        ],
    }


def _fleet_celebration(find: Mapping[str, Any]) -> dict[str, Any]:
    """Celebration layout: same 2×2 grid, the block takes the stage."""
    height = find.get("block_height")
    diff_text, diff_suffix = _fmt_si(find.get("share_difficulty"))
    ts = find.get("ts")
    return {
        "type": "four-stats",
        "refresh": WIDGET_REFRESH,
        "link": "",
        "items": [
            {
                "title": "🎉 Block found! 🎉",
                "text": f"{height}" if height is not None else "⛏️",
                "subtext": "height" if height is not None else "",
            },
            {"title": "Found by", "text": str(find.get("miner_name") or _DASH), "subtext": ""},
            {"title": "Share diff", "text": diff_text, "subtext": diff_suffix},
            {"title": "Found on", "text": _fmt_date(float(ts)) if ts else _DASH, "subtext": ""},
        ],
    }


def build_miners_widget(
    miners: list[Mapping[str, Any]],
    samples: Mapping[int, Any],
    last_seen: Mapping[int, float | None],
    now: float,
) -> dict[str, Any]:
    """The ``list`` payload: one row per enabled miner.

    Row anatomy follows the umbrelOS list item: ``subtext`` is the dim
    label line (rendered *above*), ``text`` the bright value line. So:
    label = miner name (+ model when it adds information), value =
    "1.21 TH/s · 58 °C", or "🔴 Offline · 2 h" via ``last_seen``
    (epoch of the last stored metric, only consulted for offline rows).

    Ordering: offline miners first (that's the signal worth a glance),
    then by nominal hashrate descending so rows don't reshuffle with
    every refresh, name as the tiebreaker.
    """
    entries = []
    for m in miners:
        sample = samples.get(m["id"])
        is_online = bool(sample and getattr(sample, "online", False))
        entries.append((m, sample, is_online))

    entries.sort(
        key=lambda e: (
            e[2],
            -(_nominal_ths(e[1]) if e[1] else 0.0),
            str(e[0].get("name") or "").lower(),
        )
    )

    rows: list[dict[str, str]] = []
    visible = entries if len(entries) <= LIST_MAX_ROWS else entries[: LIST_MAX_ROWS - 1]
    for m, sample, is_online in visible:
        rows.append(_miner_row(m, sample, is_online, last_seen.get(m["id"]), now))

    if len(entries) > LIST_MAX_ROWS:
        rest = entries[LIST_MAX_ROWS - 1 :]
        rest_ths = sum(
            float(s.hashrate_ths)
            for _, s, ok in rest
            if ok and s is not None and s.hashrate_ths is not None
        )
        rows.append(
            {
                "subtext": f"+{len(rest)} more miners",
                "text": _fmt_hashrate_inline(rest_ths) or _DASH,
            }
        )

    return {
        "type": "list",
        "refresh": WIDGET_REFRESH,
        "link": "",
        "noItemsText": "No miners discovered yet",
        "items": rows,
    }


def _miner_row(
    miner: Mapping[str, Any],
    sample: Any,
    is_online: bool,
    last_seen_ts: float | None,
    now: float,
) -> dict[str, str]:
    name = str(miner.get("name") or miner.get("host") or f"miner {miner.get('id')}")
    model = miner.get("model")
    label = name
    if model and str(model).lower() not in name.lower():
        label = f"{name} · {model}"

    if not is_online:
        text = "🔴 Offline"
        if last_seen_ts:
            text += f" · {_fmt_duration(now - float(last_seen_ts))}"
        return {"subtext": label, "text": text}

    parts: list[str] = []
    hr = _fmt_hashrate_inline(getattr(sample, "hashrate_ths", None))
    if hr:
        parts.append(hr)
    temp = getattr(sample, "temp_chip_c", None)
    if temp is not None:
        parts.append(f"{round(float(temp))} °C")
    return {"subtext": label, "text": " · ".join(parts) if parts else "online"}

# SPDX-License-Identifier: AGPL-3.0-only
"""Solo-mining odds for the Analytics "Predictions" widget.

Answers two questions for a fleet: how long until we beat our own best
share, and how long until we find a block. Both are the same Poisson
calculation against a different target difficulty.

Model
-----
A miner testing hashes is a Poisson process. Landing a share of
difficulty ``D`` takes ``D · 2^32`` hashes on average, so a fleet running
at ``H`` hashes/second produces them at::

    rate = H / (D · 2^32)          shares of difficulty D per second
    E[T] = 1 / rate                expected wait
    P(t) = 1 - exp(-rate · t)      probability of at least one within t

"Finding a block" is just the case where ``D`` is the network difficulty.
The memorylessness is the point worth remembering: a fleet that has been
running for a year is no closer to a block than one switched on a minute
ago.

Why this is grouped by coin
---------------------------
``rate`` only means anything when ``H`` and ``D`` describe the *same*
chain. A fleet split across BTC and BCH has two separate processes
running side by side, and collapsing them into one — total hashrate
against whichever difficulty happened to be read first — produces a
number that is not merely imprecise but arbitrary: point the same fleet
at the same pools, reorder the miners, and the answer changes. So we
partition the fleet by coin (see ``backend/coin.py``) and run the maths
once per group, each with its own hashrate and its own difficulty.

Miners that couldn't be classified are reported in their own group with
no odds attached. They are deliberately left out of every other group's
hashrate: attributing unknown hashrate to a coin would inflate that
coin's odds silently, whereas an unclassified miner is visible in the UI
and one override away from being resolved.

The "beat our best share" figure stays fleet-wide on purpose. Share
difficulty measures how far below the target a hash landed, which is a
property of the hash alone — any SHA-256 miner on any chain can set a new
record, so every miner's hashrate legitimately counts toward it.

This module is pure (no DB, no network, no FastAPI) and unit-tested in
``tests/test_predictions.py``, following ``backend/halo.py``.
"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional, Sequence

from . import coin as coin_mod

# Horizons the widget shows, as (payload key, seconds).
HORIZONS: tuple[tuple[str, float], ...] = (
    ("1h", 3600.0),
    ("24h", 86400.0),
    ("7d", 7 * 86400.0),
)

# Average hashes needed per unit of difficulty — the size of the target
# space that a difficulty-1 share covers.
HASHES_PER_DIFFICULTY = 2.0 ** 32

# TH/s → hashes/s.
TH_TO_HASHES = 1e12

# ``math.exp`` underflows gracefully, but clamping the exponent keeps the
# intent explicit: past this point P(t) is 1.0 to every digit we display.
_MAX_EXPONENT = 700.0


def _num(value: Any) -> Optional[float]:
    """Best-effort finite float; ``None`` for anything unusable."""
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def prediction_window(
    hashrate_ths: Optional[float], target_difficulty: Optional[float]
) -> Optional[dict[str, Any]]:
    """Expected time and per-horizon probabilities, or ``None``.

    Returns ``None`` — rather than zeros or infinities — whenever either
    input is missing or non-positive, so a caller with no difficulty (or a
    fleet that is entirely offline) omits the estimate instead of showing
    a confidently wrong one.
    """
    hashrate = _num(hashrate_ths)
    difficulty = _num(target_difficulty)
    if hashrate is None or hashrate <= 0:
        return None
    if difficulty is None or difficulty <= 0:
        return None

    rate = (hashrate * TH_TO_HASHES) / (difficulty * HASHES_PER_DIFFICULTY)
    if rate <= 0 or not math.isfinite(rate):
        return None

    def probability(seconds: float) -> float:
        return 1.0 - math.exp(-min(rate * seconds, _MAX_EXPONENT))

    return {
        "expected_time_s": 1.0 / rate,
        "probability": {key: probability(secs) for key, secs in HORIZONS},
    }


def group_fleet_by_coin(
    miners: Sequence[Mapping[str, Any]],
    samples: Mapping[int, Any],
    references: Mapping[str, Optional[float]],
) -> tuple[list[dict[str, Any]], float]:
    """Partition online miners by coin. Returns ``(groups, total_ths)``.

    Each group carries the hashrate its own miners contribute, the network
    difficulty to grade them against, and how that difficulty was obtained:

      * ``stratum``  — reported by the miners themselves. Preferred: it is
        the value the pool is grading their shares with, so the odds stay
        consistent with what the miner sees, and it needs no network call.
        With several miners on one coin we take the highest reported value,
        which is the freshest one across a retarget boundary (a miner that
        hasn't reconnected can still be advertising the previous epoch).
      * ``explorer`` — this coin's difficulty from ``coin_difficulty``,
        used when no miner in the group reports one (Braiins, LuxOS and
        Canaan never do).

    ``total_ths`` is the whole online fleet including unclassified miners,
    so the caller can display a fleet total that still adds up even when
    the per-coin groups don't cover everything.

    Groups come back ordered by hashrate, largest first — the order the UI
    shows its sub-tabs in, so the coin the user cares most about leads.
    Offline miners are skipped entirely: this is a live estimate, and a
    powered-down device contributes no hashes.
    """
    buckets: dict[Optional[str], dict[str, Any]] = {}
    total_ths = 0.0

    for miner in miners:
        sample = samples.get(miner["id"])
        if not sample or not getattr(sample, "online", False):
            continue

        hashrate = _num(getattr(sample, "hashrate_ths", None)) or 0.0
        if hashrate > 0:
            total_ths += hashrate

        coin_id, source = coin_mod.classify(miner, sample, references)
        bucket = buckets.setdefault(
            coin_id,
            {
                "coin": coin_id,
                "hashrate_ths": 0.0,
                "miner_count": 0,
                "miner_ids": [],
                "coin_sources": set(),
                "_stratum_difficulty": None,
            },
        )
        bucket["hashrate_ths"] += hashrate
        bucket["miner_count"] += 1
        bucket["miner_ids"].append(miner["id"])
        if source:
            bucket["coin_sources"].add(source)

        reported = _num(getattr(sample, "network_difficulty", None))
        if reported and reported > 0:
            current = bucket["_stratum_difficulty"]
            if current is None or reported > current:
                bucket["_stratum_difficulty"] = reported

    groups: list[dict[str, Any]] = []
    for coin_id, bucket in buckets.items():
        stratum_difficulty = bucket.pop("_stratum_difficulty")
        sources = bucket.pop("coin_sources")

        if coin_id is None:
            difficulty, difficulty_source = None, None
        elif stratum_difficulty:
            difficulty, difficulty_source = stratum_difficulty, "stratum"
        else:
            difficulty = _num(references.get(coin_id))
            difficulty_source = "explorer" if difficulty and difficulty > 0 else None

        groups.append(
            {
                **bucket,
                "label": coin_mod.LABELS.get(coin_id) if coin_id else None,
                "ticker": coin_mod.TICKERS.get(coin_id) if coin_id else None,
                "hashrate_ths": round(bucket["hashrate_ths"], 4),
                "coin_sources": sorted(sources),
                "network_difficulty": difficulty,
                "difficulty_source": difficulty_source,
                "find_block": prediction_window(bucket["hashrate_ths"], difficulty),
            }
        )

    # Unclassified last regardless of size: it is a to-do item, not a
    # result, and shouldn't outrank a real coin in the sub-tab strip.
    groups.sort(key=lambda g: (g["coin"] is None, -g["hashrate_ths"]))
    return groups, round(total_ths, 4)


def build_prediction_payload(
    miners: Sequence[Mapping[str, Any]],
    samples: Mapping[int, Any],
    references: Mapping[str, Optional[float]],
    best_alltime: Mapping[str, Any] | None,
    forced_coin: Optional[str] = None,
    forced_difficulty: Optional[float] = None,
) -> dict[str, Any]:
    """Build the ``GET /api/fleet/prediction`` body.

    ``forced_coin`` switches the widget from "what are my real odds" to
    "what if the whole fleet mined this coin" — a what-if that deliberately
    uses the total fleet hashrate against ``forced_difficulty``, and so
    answers a different question from the per-coin groups. In that mode the
    groups are still returned (the UI keeps them for its sub-tabs) but the
    top-level ``find_block`` reflects the what-if.

    ``predictions.find_block`` and the top-level ``network_difficulty`` are
    retained for backward compatibility: they carry the dominant group's
    numbers so a frontend built before ``groups`` existed keeps working
    against a newer backend, which matters during an Umbrel upgrade when a
    stale bundle may briefly be served.
    """
    groups, total_ths = group_fleet_by_coin(miners, samples, references)
    any_hashrate = total_ths > 0

    best_value = _num((best_alltime or {}).get("value")) if best_alltime else None
    beat_best = prediction_window(total_ths, best_value)

    # Dominant = the largest classified group. Unclassified miners never
    # stand in for a coin, so a fleet we know nothing about reports no
    # legacy find_block rather than a made-up one.
    dominant = next((g for g in groups if g["coin"] is not None), None)

    if forced_coin:
        headline_difficulty = _num(forced_difficulty)
        headline = prediction_window(total_ths, headline_difficulty)
    elif dominant:
        headline_difficulty = dominant["network_difficulty"]
        headline = dominant["find_block"]
    else:
        headline_difficulty = None
        headline = None

    return {
        "fleet_hashrate_ths": total_ths if any_hashrate else None,
        "best_alltime": dict(best_alltime) if best_alltime else None,
        "network_difficulty": headline_difficulty,
        "coin": forced_coin or "auto",
        "groups": groups,
        "predictions": {
            "beat_best": beat_best,
            "find_block": headline,
        },
    }

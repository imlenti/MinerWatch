# SPDX-License-Identifier: AGPL-3.0-only
"""Payload builder for the read-only fleet endpoint ``GET /api/halo``.

An external display device polls this endpoint and renders the numbers
it returns; it performs no calculations of its own, so every field is
computed here. Like ``umbrel_widgets``, the logic is a pure function of
plain inputs (miner rows, live poller samples, best-record dicts) so it
unit-tests without FastAPI or the DB. The one deliberate exception is the
``share_seq`` counter, which must remember its previous value across
calls — documented at :func:`_advance_share_seq`.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional

from . import coin as coin_mod

# Lower bound of the log scale: the pool's minimum share difficulty. A
# constant is fine here — pool floors don't move much and the consumer
# only needs a sane lower bound.
DEFAULT_FLOOR_DIFF = 1000.0

# Last-resort upper bound, used only when no miner reports a live network
# difficulty AND the cached BTC lookup is also empty. Picked to be in the
# right order of magnitude; a real value from stratum replaces it the
# moment any miner reports one.
DEFAULT_NET_DIFF = 1.2e14


# ---------------------------------------------------------------------------
# share_seq — monotonic "new share" counter
# ---------------------------------------------------------------------------
# The consumer redraws whenever ``share_seq`` changes, so the value must
# be monotonic (it may never travel backwards) and should tick on every
# accepted share. We derive it from the fleet-wide sum of each miner's
# cumulative accepted-share counter. That sum normally only grows, but a
# single miner rebooting resets its counter to zero, which would make the
# naive sum jump *down* and break the "never backwards" rule.
#
# So we keep a running accumulator: add only the positive deltas of the
# raw sum, ignore the dips. A reboot therefore pauses the counter for a
# moment and it resumes the instant new shares arrive, without ever
# rewinding. The state is per-process — the same lifetime as the
# ``poller.last_results`` snapshot that feeds it (MinerWatch runs a single
# app process) — and a restart simply re-seeds the baseline, which is
# harmless because the consumer only cares that the number changed.
_seq_state: dict[str, int] = {"accum": 0, "last_raw": 0}


def _advance_share_seq(raw_accepted_sum: int) -> int:
    """Fold a fresh fleet-wide accepted-share sum into the monotonic counter."""
    delta = raw_accepted_sum - _seq_state["last_raw"]
    if delta > 0:
        _seq_state["accum"] += delta
    _seq_state["last_raw"] = raw_accepted_sum
    return _seq_state["accum"]


def reset_share_seq() -> None:
    """Reset the share_seq accumulator. Test-only helper."""
    _seq_state["accum"] = 0
    _seq_state["last_raw"] = 0


def _num(value: Any) -> float | None:
    """Best-effort float; ``None`` for missing or unparseable values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pick_last_share(
    live_shares: Mapping[int, Mapping[str, Any]],
    latest_share: Mapping[str, Any] | None,
    best_diff: float,
    session: Mapping[str, Any],
) -> tuple[float, str, int | None]:
    """Pick (difficulty, miner_name, miner_id) of the most recent share.

    Prefers the live per-share feed (the newest *submitted* share across
    AxeOS miners); falls back to the most recent persisted notable share,
    then to the session best record. The live feed is far more frequent,
    so on an AxeOS fleet it effectively always wins.

    The miner id rides along so the caller can grade this share against the
    difficulty of the chain it was actually mined on; it is ``None`` when
    the share came from a source that doesn't identify a miner.
    """
    live_ts: float | None = None
    live_diff: float | None = None
    live_name: Any = None
    live_id: int | None = None
    for miner_id, lv in (live_shares or {}).items():
        ts = _num(lv.get("last_ts"))
        diff = _num(lv.get("last_diff"))
        if ts is None or diff is None:
            continue
        if live_ts is None or ts > live_ts:
            live_ts, live_diff, live_name = ts, diff, lv.get("name")
            live_id = miner_id

    notable_ts = _num(latest_share.get("ts")) if latest_share else None
    notable_diff = _num(latest_share.get("share_difficulty")) if latest_share else None
    notable_name = latest_share.get("name") if latest_share else None
    notable_id = latest_share.get("miner_id") if latest_share else None

    if live_diff is not None and (notable_ts is None or live_ts >= notable_ts):
        return live_diff, str(live_name or "—"), live_id
    if notable_diff is not None:
        try:
            resolved_id = int(notable_id) if notable_id is not None else None
        except (TypeError, ValueError):
            resolved_id = None
        return notable_diff, str(notable_name or "—"), resolved_id
    return best_diff, str(session.get("miner_name") or "—"), None


def build_halo_payload(
    miners: list[Mapping[str, Any]],
    samples: Mapping[int, Any],
    best: Mapping[str, Any],
    top_records: list[Mapping[str, Any]],
    latest_share: Mapping[str, Any] | None,
    net_diff_fallback: float | None,
    live_shares: Mapping[int, Mapping[str, Any]] | None = None,
    btc_price: float | None = None,
    btc_change: float | None = None,
    floor: float = DEFAULT_FLOOR_DIFF,
    references: Mapping[str, Optional[float]] | None = None,
) -> dict[str, Any]:
    """Build the exact JSON object the ``/api/halo`` consumer expects.

    ``miners``         enabled miner rows (``db.list_miners(only_enabled=True)``)
    ``samples``        poller live snapshot, keyed by miner id (``poller.last_results``)
    ``best``           ``db.get_fleet_best_records()`` → ``{"session":…, "alltime":…}``
    ``top_records``    ``db.get_fleet_best_records_ranked("alltime", 3)``
    ``latest_share``   fleet-wide most recent notable share, or ``None``
    ``net_diff_fallback`` cached BTC network difficulty, used only when no
                          miner reports a live one (never triggers a fetch)
    ``live_shares``    per-miner live per-share state for AxeOS miners,
                       keyed by miner id, each
                       ``{"submitted_total": int, "last_diff": float,
                       "last_ts": float, "name": str}``. Drives a
                       per-share ``last_diff`` and ``share_seq``; miners
                       without an entry fall back to poller aggregates.
    ``btc_price``      current BTC price in USD, or ``None``; emitted as
                       ``btc_price`` rounded to an integer, omitted when None
    ``btc_change``     signed 24h % change, or ``None``; emitted as
                       ``btc_change`` (2 decimals), omitted when None
    ``references``     per-coin network difficulties (``coin_difficulty.
                       cached_references()``), used to work out which chain
                       the displayed share belongs to on a mixed fleet.
                       Optional: without it the gauge falls back to the
                       fleet-wide behaviour described under ``net_diff``.
    """
    live_shares = live_shares or {}
    references = references or {}
    total_ths = 0.0
    online_count = 0
    raw_seq = 0
    net_live: float | None = None
    # Live stratum difficulty per miner, so the gauge can pick the one
    # belonging to the chain the displayed share came from.
    net_by_miner: dict[int, float] = {}
    coin_by_miner: dict[int, str | None] = {}

    for m in miners:
        sample = samples.get(m["id"])
        online = bool(sample and getattr(sample, "online", False))
        live = live_shares.get(m["id"])

        if online:
            online_count += 1
            # hashrate_ths is already TH/s in the poller sample — no unit
            # conversion needed (the metrics table stores the same value).
            hr = _num(getattr(sample, "hashrate_ths", None))
            if hr is not None:
                total_ths += hr
            nd = _num(getattr(sample, "network_difficulty", None))
            if nd and (net_live is None or nd > net_live):
                net_live = nd
            if nd:
                net_by_miner[m["id"]] = nd
            coin_by_miner[m["id"]], _ = coin_mod.classify(m, sample, references)

        # share_seq source, per miner: prefer the live per-share counter
        # (AxeOS: +1 per submitted share) and fall back to the poller's
        # cumulative accepted count for miners with no live stream.
        if live is not None:
            raw_seq += int(live.get("submitted_total") or 0)
        elif online:
            acc = getattr(sample, "accepted", None)
            if acc is not None:
                try:
                    raw_seq += int(acc)
                except (TypeError, ValueError):
                    pass

    session = best.get("session") or {}
    alltime = best.get("alltime") or {}
    best_diff = _num(session.get("value")) or 0.0
    best_alltime = _num(alltime.get("value")) or best_diff

    top = [_num(r.get("value")) or 0.0 for r in (top_records or [])][:3]
    top += [0.0] * (3 - len(top))

    last_diff, miner_name, last_miner_id = _pick_last_share(
        live_shares, latest_share, best_diff, session
    )

    # The gauge draws last_diff against net_diff, so the two have to belong
    # to the same chain. On a fleet split across BTC and BCH the old rule —
    # take the highest difficulty anywhere in the fleet — always picked BTC
    # and made every BCH share look ~1000× further from a block than it was.
    # Prefer the difficulty of the miner that produced the share we're
    # showing: its own stratum reading first, then its coin's reference.
    net_diff: float | None = None
    if last_miner_id is not None:
        net_diff = net_by_miner.get(last_miner_id)
        if net_diff is None:
            last_coin = coin_by_miner.get(last_miner_id)
            if last_coin:
                net_diff = _num(references.get(last_coin))
    net_diff = net_diff or net_live or _num(net_diff_fallback) or DEFAULT_NET_DIFF

    payload: dict[str, Any] = {
        "last_diff": last_diff,
        "best_diff": best_diff,
        "best_alltime": best_alltime,
        "net_diff": net_diff,
        "floor": float(floor),
        "top": top,
        "miner": miner_name,
        "ths": round(total_ths, 3),
        "miners": online_count,
        "online": online_count > 0,
        "share_seq": _advance_share_seq(raw_seq),
    }

    # Bitcoin price block shown on the device. Included only when known so
    # a cold/failed price cache leaves the device on its last value rather
    # than flashing $0.
    usd = _num(btc_price)
    if usd is not None:
        payload["btc_price"] = round(usd)
    chg = _num(btc_change)
    if chg is not None:
        payload["btc_change"] = round(chg, 2)

    return payload

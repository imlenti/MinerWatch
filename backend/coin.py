# SPDX-License-Identifier: AGPL-3.0-only
"""Which SHA-256 coin is each miner actually working on?

MinerWatch supports fleets that mine more than one SHA-256 chain at the
same time — typically some devices on BTC and others on BCH. Every
fleet-wide number that pairs *hashrate* with a *network difficulty* is
wrong unless the two sides refer to the same chain, so several call sites
need to know, per miner, which coin its hashrate is being spent on:

  * ``backend/predictions.py`` groups the fleet by coin so the Analytics
    "Find a block (solo)" odds use each coin's own hashrate and difficulty
    instead of the whole fleet against one arbitrary difficulty;
  * ``backend/halo.py`` grades the last share against the difficulty of
    the chain that share was actually mined on;
  * ``backend/poller.py`` compares a best share to the right chain's
    difficulty before declaring a block found.

This module is the single place that answers the question. It is pure —
no DB, no network, no FastAPI — so it unit-tests directly (see
``tests/test_coin.py``), following the same shape as ``backend/halo.py``.

Detection cascade
-----------------
Each step is tried in order and the first confident answer wins. Every
result carries the *source* that produced it, so the UI can show how a
miner was classified and the user knows what to trust:

1. ``override``  — the user said so explicitly (``miners.coin_override``).
   Always wins, including over a live stratum reading, because it is the
   escape hatch for anything the automatic steps get wrong.
2. ``stratum``   — the miner reports the network difficulty it is mining
   against (AxeOS ``networkDifficulty``, NMAxe ``networkDiff``). BTC and
   BCH difficulties sit orders of magnitude apart, so matching the
   reported value against the live reference difficulties is unambiguous.
   This is the strongest automatic signal: it comes from the pool itself,
   via the miner, and it tracks a failover to another chain immediately.
3. ``address``   — the pool username often starts with the payout address,
   and some address formats name their chain unambiguously: the CashAddr
   ``bitcoincash:`` prefix is BCH, a ``bc1`` bech32 address is BTC.
   Legacy ``1…``/``3…`` addresses are valid on both chains and are
   deliberately NOT used.
4. ``pool``      — best-effort token match on the pool hostname
   (``bch.example.org``, ``bitcoincash-solo.example``). Weakest signal,
   and the reason the override exists.

When nothing matches, the miner is left unclassified (``None``). Callers
must treat that as "don't know" and keep it out of any per-coin maths
rather than guessing — a wrong guess silently corrupts the odds, while an
unclassified miner is visible in the UI and one click away from being
fixed with an override.

Only the Bitaxe driver family (Bitaxe, NerdQAxe/NMAxe, NerdOctaxe,
BitForge) reports a stratum difficulty today; Braiins, LuxOS and Canaan
leave it ``None`` and therefore depend on steps 3-4 or on an override.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping, Optional, Tuple

# Coins MinerWatch can tell apart. Both are SHA-256, so a given miner can
# hash for either one and only the network difficulty differs — which is
# precisely why the two must never be mixed in the same calculation.
COINS: Tuple[str, ...] = ("btc", "bch")

# Display strings, kept here so backend payloads and the UI agree.
LABELS = {"btc": "Bitcoin", "bch": "Bitcoin Cash"}
TICKERS = {"btc": "BTC", "bch": "BCH"}

# Detection sources, in descending order of trust. Emitted in API payloads
# as ``coin_source`` so the UI can distinguish "the pool told us" from
# "we guessed from the hostname".
SOURCE_OVERRIDE = "override"
SOURCE_STRATUM = "stratum"
SOURCE_ADDRESS = "address"
SOURCE_POOL = "pool"

# How far a reported difficulty may sit from a reference before we refuse
# to call it a match, measured in decades (log10 of the ratio). 0.7 ≈ a
# factor of 5 in either direction — far wider than any single retarget
# (BTC moves at most ±4× per retarget and in practice a few percent; BCH's
# ASERT DAA moves continuously in small steps), yet far tighter than the
# ~3 orders of magnitude that separate the BTC and BCH difficulties. The
# gap between "generous" and "ambiguous" is wide enough that this constant
# is not a tuning knob in practice.
_MATCH_TOLERANCE_DECADES = 0.7

# Hostname tokens. Checked longest-first and against token boundaries so
# "bitcoincash" can never be read as "bitcoin".
_HOST_TOKENS = (
    ("bitcoincash", "bch"),
    ("bcash", "bch"),
    ("bch", "bch"),
    ("bitcoin", "btc"),
    ("btc", "btc"),
)

# Splits a hostname into comparable tokens: dots, dashes, underscores and
# digits all act as separators, so "bch-solo.pool.org" and "solo2bch.example"
# both surface a clean "bch".
_TOKEN_SPLIT = re.compile(r"[^a-z]+")


def normalize(value: Any) -> Optional[str]:
    """Coerce arbitrary input to a supported coin id, or ``None``.

    Used both for validating what the user sends to the override endpoint
    and for reading the stored column back, so an unknown or corrupt value
    degrades to "unclassified" instead of poisoning a calculation.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    return text if text in COINS else None


def classify_by_difficulty(
    difficulty: Any, references: Mapping[str, Optional[float]]
) -> Optional[str]:
    """Match a reported network difficulty to the closest reference coin.

    ``references`` maps coin id → that coin's current network difficulty
    (from ``coin_difficulty``); entries may be ``None`` when a lookup has
    not succeeded yet, and are skipped. Comparison is done in log space so
    it is scale-free: what matters is the *ratio* to each reference, not
    the absolute gap, which would otherwise always favour the larger coin.

    Returns ``None`` when the value is unusable or sits further than
    :data:`_MATCH_TOLERANCE_DECADES` from every reference — a difficulty
    that matches nothing is far more likely to be a third chain or a
    parsing error than a coin we know about.
    """
    try:
        value = float(difficulty)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None

    best_coin: Optional[str] = None
    best_distance = float("inf")
    for coin in COINS:
        ref = references.get(coin)
        try:
            ref_value = float(ref) if ref is not None else 0.0
        except (TypeError, ValueError):
            continue
        if not math.isfinite(ref_value) or ref_value <= 0:
            continue
        distance = abs(math.log10(value / ref_value))
        if distance < best_distance:
            best_coin, best_distance = coin, distance

    if best_coin is None or best_distance > _MATCH_TOLERANCE_DECADES:
        return None
    return best_coin


def classify_by_address(user: Any) -> Optional[str]:
    """Infer the coin from the payout address in a pool username.

    Solo miners set the pool user to their payout address (often followed
    by ``.workername``), and two address formats identify their chain on
    sight:

      * ``bitcoincash:…`` — the CashAddr URI prefix, BCH only.
      * ``bc1…``          — bech32 with Bitcoin's mainnet HRP, BTC only.

    Bare CashAddr (``q…``/``p…`` with no prefix) is intentionally ignored:
    it is not anchored to a chain name and false positives here would be
    both silent and hard to explain. Legacy ``1…``/``3…`` addresses are
    valid on both chains and are likewise never used.
    """
    if not user:
        return None
    text = str(user).strip().lower()
    if not text:
        return None
    if text.startswith("bitcoincash:"):
        return "bch"
    if text.startswith("bc1"):
        return "btc"
    return None


def classify_by_pool_url(url: Any) -> Optional[str]:
    """Best-effort coin detection from a pool hostname.

    Strips any scheme and port, then looks for a coin token among the
    hostname's alphabetic runs. "bitcoincash" and "bcash" are tested
    before "bitcoin"/"btc" so a BCH host is never misread as BTC.

    This is the weakest signal in the cascade and is documented as such:
    plenty of pools carry no coin hint in their hostname at all, and a
    fleet that relies on this step should really be using an override.
    """
    if not url:
        return None
    text = str(url).strip().lower()
    if not text:
        return None
    # Drop scheme ("stratum+tcp://host:port") and any path, then the port.
    text = text.split("://", 1)[-1].split("/", 1)[0]
    host = text.rsplit(":", 1)[0] if ":" in text else text
    tokens = set(t for t in _TOKEN_SPLIT.split(host) if t)
    for token, coin in _HOST_TOKENS:
        if token in tokens:
            return coin
    # Fall back to a substring scan for hosts that glue words together
    # without a separator ("solobchpool.example"). Ordered longest-first
    # by _HOST_TOKENS, so "bitcoincash" wins over "bitcoin".
    for token, coin in _HOST_TOKENS:
        if token in host:
            return coin
    return None


def active_pool(sample: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(url, user)`` of the pool slot a miner is currently mining on.

    Prefers the structured ``pools`` list, picking the slot the firmware
    flags as active; falls back to the first entry, and then to the legacy
    ``pool_url``/``worker`` scalars that every driver still fills. Returns
    ``(None, None)`` for a sample that carries no pool information.
    """
    if sample is None:
        return None, None

    pools = getattr(sample, "pools", None) or []
    chosen = None
    for pool in pools:
        if getattr(pool, "active", None):
            chosen = pool
            break
    if chosen is None and pools:
        chosen = pools[0]
    if chosen is not None:
        url = getattr(chosen, "url", None)
        user = getattr(chosen, "user", None)
        if url or user:
            return url, user

    return getattr(sample, "pool_url", None), getattr(sample, "worker", None)


def classify(
    miner: Mapping[str, Any] | None,
    sample: Any,
    references: Mapping[str, Optional[float]],
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve ``(coin, source)`` for one miner. See the module docstring.

    ``miner`` is the DB row (only ``coin_override`` is read, so a plain
    dict works in tests), ``sample`` the live poller sample, and
    ``references`` the current per-coin network difficulties. Returns
    ``(None, None)`` when no step is confident — never a guess.

    Note that the override is honoured even when the miner is offline or
    has no sample at all, so a powered-down device keeps its identity in
    the UI instead of flickering into "unknown".
    """
    override = normalize((miner or {}).get("coin_override"))
    if override:
        return override, SOURCE_OVERRIDE

    if sample is not None:
        by_difficulty = classify_by_difficulty(
            getattr(sample, "network_difficulty", None), references
        )
        if by_difficulty:
            return by_difficulty, SOURCE_STRATUM

        url, user = active_pool(sample)
        by_address = classify_by_address(user)
        if by_address:
            return by_address, SOURCE_ADDRESS

        by_pool = classify_by_pool_url(url)
        if by_pool:
            return by_pool, SOURCE_POOL

    return None, None

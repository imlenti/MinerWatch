# SPDX-License-Identifier: AGPL-3.0-only
"""SQLite schema and access helpers (aiosqlite).

Tables:
- miners            device registry (auto-discovery + manual)
- metrics           time-series samples (hashrate, temp, power, etc.)
- alerts            alert history
- push_subscriptions browser clients registered for Web Push
- settings          runtime config overrides
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import aiosqlite

from .config import db_path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS miners (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    family          TEXT NOT NULL,        -- bitaxe | canaan | braiins
    model           TEXT,
    host            TEXT NOT NULL,        -- IP or hostname
    port            INTEGER,
    mac             TEXT UNIQUE,          -- stable key in case the IP changes
    enabled         INTEGER NOT NULL DEFAULT 1,
    notes           TEXT,
    fan_threshold_c REAL,                 -- per-miner alert threshold override (optional)
    -- MinerWatch auto-fan: server-side PID that nudges the fan speed
    -- to keep chip temp near `auto_target_c`.
    -- mode: 'manual' (use a fixed fan_pct), 'firmware' (delegate to Avalon),
    --       'minerwatch' (server-side PID based on target/floor)
    fan_mode        TEXT DEFAULT 'firmware',
    auto_target_c   REAL,                 -- target temperature for minerwatch mode
    fan_min_override INTEGER,             -- minimum percent override (default 15)
    fan_max_override INTEGER,             -- maximum percent override (default 100)
    -- Guardian (runtime frequency governor). Per-miner opt-in + the
    -- frequency ceiling/floor it operates within. All thresholds/steps
    -- are global (see GuardianCfg) and only these per-device knobs live here.
    -- NOTE: keep these comments free of any semicolon — SCHEMA_SQL is split
    -- statement-by-statement on the semicolon separator.
    guardian_enabled        INTEGER DEFAULT 0,  -- 0/1 per-miner opt-in
    guardian_max_freq_mhz   INTEGER,            -- ceiling (defaults to current freq at enable time)
    guardian_freq_floor_mhz INTEGER,            -- optional floor override (NULL → global default)
    guardian_temp_source    TEXT,               -- vr (default) or chip — which sensor governs frequency
    guardian_max_temp_c     REAL,               -- per-miner max temp / high threshold (NULL → source default)
    last_seen_ts    INTEGER,
    last_status     TEXT,                 -- online | offline | error
    extra           TEXT,                 -- free-form JSON
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_miners_host ON miners(host);

CREATE TABLE IF NOT EXISTS metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id        INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    hashrate_ths    REAL,
    power_w         REAL,
    temp_chip_c     REAL,
    temp_vr_c       REAL,
    fan_rpm         INTEGER,
    fan_pct         REAL,
    frequency_mhz   REAL,
    voltage_mv      REAL,
    uptime_s        INTEGER,
    accepted        INTEGER,
    rejected        INTEGER,
    best_difficulty REAL,
    pool_url        TEXT,
    worker          TEXT,
    raw             TEXT,                 -- original payload as JSON
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_miner_ts ON metrics(miner_id, ts);
CREATE INDEX IF NOT EXISTS idx_metrics_ts ON metrics(ts);

-- Rollup tier #1: 1-minute aggregates of `metrics`.
-- Populated by the `rollup_to_1m` job, retention much longer than raw.
-- `ts` is the bucket-start unix timestamp (rounded down to the minute).
CREATE TABLE IF NOT EXISTS metrics_1m (
    miner_id        INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    hashrate_ths    REAL,                 -- AVG of the bucket
    power_w         REAL,                 -- AVG
    temp_chip_c     REAL,                 -- AVG
    temp_chip_max_c REAL,                 -- MAX (peak in the bucket)
    temp_vr_c       REAL,                 -- AVG
    fan_rpm         INTEGER,              -- AVG
    fan_pct         REAL,                 -- AVG
    frequency_mhz   REAL,                 -- AVG
    voltage_mv      REAL,                 -- AVG
    uptime_s        INTEGER,              -- MAX (monotonic counter)
    accepted        INTEGER,              -- MAX
    rejected        INTEGER,              -- MAX
    best_difficulty REAL,                 -- MAX
    sample_count    INTEGER NOT NULL,     -- raw samples that fed this bucket
    PRIMARY KEY (miner_id, ts),
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_1m_ts ON metrics_1m(ts);

-- Rollup tier #2: 1-hour aggregates of `metrics_1m`.
CREATE TABLE IF NOT EXISTS metrics_1h (
    miner_id        INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    hashrate_ths    REAL,
    power_w         REAL,
    temp_chip_c     REAL,
    temp_chip_max_c REAL,
    temp_vr_c       REAL,
    fan_rpm         INTEGER,
    fan_pct         REAL,
    frequency_mhz   REAL,
    voltage_mv      REAL,
    uptime_s        INTEGER,
    accepted        INTEGER,
    rejected        INTEGER,
    best_difficulty REAL,
    sample_count    INTEGER NOT NULL,
    PRIMARY KEY (miner_id, ts),
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_1h_ts ON metrics_1h(ts);

CREATE TABLE IF NOT EXISTS alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id        INTEGER,
    ts              INTEGER NOT NULL,
    severity        TEXT NOT NULL,        -- info | warning | critical
    code            TEXT NOT NULL,        -- temp_chip | temp_vr | offline | recovered
    message         TEXT NOT NULL,
    acknowledged    INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint        TEXT UNIQUE NOT NULL,
    p256dh          TEXT NOT NULL,
    auth            TEXT NOT NULL,
    user_agent      TEXT,
    created_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

-- Best-share records.
-- One row per (miner_id, scope). `scope` is either 'session' (best since
-- the last detected miner reboot) or 'alltime' (best ever observed by
-- MinerWatch — outlives miner reboots and even firmware re-flashes,
-- since the value is stored here, not in the miner's own NVS).
-- `uptime_at_record` lets us detect a session reset: when the live
-- uptime drops below the value we stored, we know the miner rebooted
-- and we can clear the session row.
CREATE TABLE IF NOT EXISTS best_records (
    miner_id        INTEGER NOT NULL,
    scope           TEXT NOT NULL,        -- 'session' | 'alltime'
    value           REAL NOT NULL,        -- best difficulty in raw units
    ts              INTEGER NOT NULL,     -- unix ts when the record was set
    uptime_at_record INTEGER,             -- miner uptime when set (for reset detection)
    PRIMARY KEY (miner_id, scope),
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_best_records_miner ON best_records(miner_id);

-- Solo-mining block-found events. Each row is a share whose difficulty
-- was greater than or equal to the Bitcoin network difficulty at the
-- time the share was seen — i.e. the miner has effectively found a
-- block. Statistically rare for home gear, very special for the owner.
--
-- We store both the share difficulty and the network difficulty at
-- the moment of discovery so the home page can show how big the win
-- actually was (e.g. "share 130 T vs network 125 T").
-- block_height is optional and may be filled in later via a
-- block-explorer lookup. It is nullable on insert, so we never block
-- the alert pipeline on an external API call.
-- NOTE for future edits of this schema: keep comments free of any
-- semicolon character, even inside an SQL --line comment. The setup
-- code splits SCHEMA_SQL statement-by-statement on the semicolon
-- separator, and a stray one in a comment is interpreted as the end
-- of a statement, leaving the rest as garbage SQL.
CREATE TABLE IF NOT EXISTS block_finds (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id            INTEGER,
    miner_name          TEXT NOT NULL,
    ts                  INTEGER NOT NULL,
    share_difficulty    REAL NOT NULL,
    network_difficulty  REAL NOT NULL,
    block_height        INTEGER,
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_block_finds_ts ON block_finds(ts);

-- Notable shares — the "near-block Hall of Fame". Fed by the live log
-- streamer (backend/log_streamer.py), NOT the REST poller: every ASIC
-- result whose difficulty clears a floor is recorded here, so the user
-- keeps the full history of high shares, not just the running record.
--
-- We deliberately store ONLY the numbers. The source log line also
-- carries the payout address and worker name — those are parsed and
-- discarded upstream and must never land in this table.
--
-- `accepted` is 1/0 once the pool's verdict arrives, or NULL while the
-- submit is still in flight. Rows are capped per miner (top-by-diff) by
-- the insert helper, so the table stays small.
CREATE TABLE IF NOT EXISTS notable_shares (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    miner_id        INTEGER NOT NULL,
    ts              INTEGER NOT NULL,
    share_difficulty REAL NOT NULL,
    pool_target     REAL,
    accepted        INTEGER,              -- 1 | 0 | NULL (pending)
    FOREIGN KEY (miner_id) REFERENCES miners(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notable_shares_miner
    ON notable_shares(miner_id, share_difficulty DESC);

-- ---------- Donate hashrate ----------
-- One row per "Donate hashrate" action. A single action may span
-- several miners (see donation_miners). ends_ts is an ABSOLUTE epoch so
-- "time remaining" and the boot catch-up survive a restart. status is a
-- roll-up of the child rows: active | completed | stopped | partial_error
-- NOTE: keep these comments free of any semicolon — SCHEMA_SQL is split
-- statement-by-statement on the semicolon separator.
CREATE TABLE IF NOT EXISTS donations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts   INTEGER NOT NULL,
    ends_ts      INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'active',
    worker_name  TEXT NOT NULL,
    note         TEXT
);

-- One row per miner inside a donation. prev_pool holds the JSON snapshot
-- of the miner's pool config before the switch — the spine of the
-- feature, used to restore on revert and on boot catch-up.
-- status: active | unreachable | reverted | error
CREATE TABLE IF NOT EXISTS donation_miners (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    donation_id   INTEGER NOT NULL,
    miner_id      INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active',
    prev_pool     TEXT NOT NULL,
    applied_ts    INTEGER,
    reverted_ts   INTEGER,
    last_error    TEXT,
    FOREIGN KEY (donation_id) REFERENCES donations(id) ON DELETE CASCADE,
    FOREIGN KEY (miner_id)    REFERENCES miners(id)    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_donation_miners_status
    ON donation_miners(status);
CREATE INDEX IF NOT EXISTS idx_donation_miners_donation
    ON donation_miners(donation_id);
"""


def now_ts() -> int:
    return int(time.time())


# Module-level variable that remembers which journal mode worked.
# On the first `connect()` we try WAL; if it fails we stay on MEMORY for
# the lifetime of the process to avoid leaving stray journal files behind.
_journal_mode: str | None = None


@asynccontextmanager
async def connect() -> AsyncIterator[aiosqlite.Connection]:
    """Open an aiosqlite connection in autocommit + selected journal mode.

    isolation_level=None puts aiosqlite in autocommit: writes are
    confirmed immediately without orchestrating BEGIN/COMMIT (some
    filesystems don't tolerate implicit transactions well).
    """
    global _journal_mode

    if _journal_mode is None:
        # Probe: try WAL, fall back to MEMORY. Use an innocuous SELECT
        # to verify the mode is accepted, without polluting the DB.
        for mode in ("WAL", "MEMORY"):
            try:
                test = await aiosqlite.connect(str(db_path()), isolation_level=None)
                try:
                    await test.execute(f"PRAGMA journal_mode = {mode}")
                    await test.execute("SELECT 1")
                    _journal_mode = mode
                    break
                finally:
                    await test.close()
            except Exception:  # noqa: BLE001
                continue
        if _journal_mode is None:
            _journal_mode = "MEMORY"

    conn = await aiosqlite.connect(str(db_path()), isolation_level=None)
    conn.row_factory = aiosqlite.Row
    try:
        try:
            await conn.execute("PRAGMA foreign_keys = ON")
        except Exception:  # noqa: BLE001
            pass
        try:
            await conn.execute(f"PRAGMA journal_mode = {_journal_mode}")
        except Exception:  # noqa: BLE001
            pass
        yield conn
    finally:
        await conn.close()


def _init_db_sync() -> None:
    """Create the schema using synchronous sqlite3.

    We use native sqlite3 for setup because on some filesystems
    (sandboxes, network mounts) aiosqlite has specific issues with
    CREATE TABLE inside implicit transactions. The runtime phase
    stays async.
    """
    import sqlite3

    conn = sqlite3.connect(str(db_path()), isolation_level=None)
    try:
        for mode in ("WAL", "MEMORY"):
            try:
                conn.execute(f"PRAGMA journal_mode = {mode}").fetchall()
                break
            except sqlite3.OperationalError:
                continue
        # executescript opens an implicit transaction that some
        # filesystems dislike. Execute statement-by-statement instead.
        for stmt in [s.strip() for s in SCHEMA_SQL.split(";") if s.strip()]:
            conn.execute(stmt)

        # Idempotent migrations for DBs that already exist (older
        # versions without these columns). ADD COLUMN is idempotent
        # only if the column is missing — we swallow the error otherwise.
        for column_def in [
            "ALTER TABLE miners ADD COLUMN fan_mode TEXT DEFAULT 'firmware'",
            "ALTER TABLE miners ADD COLUMN auto_target_c REAL",
            "ALTER TABLE miners ADD COLUMN fan_min_override INTEGER",
            "ALTER TABLE miners ADD COLUMN fan_max_override INTEGER",
            # Guardian (runtime frequency governor) per-miner knobs.
            "ALTER TABLE miners ADD COLUMN guardian_enabled INTEGER DEFAULT 0",
            "ALTER TABLE miners ADD COLUMN guardian_max_freq_mhz INTEGER",
            "ALTER TABLE miners ADD COLUMN guardian_freq_floor_mhz INTEGER",
            "ALTER TABLE miners ADD COLUMN guardian_temp_source TEXT",
            "ALTER TABLE miners ADD COLUMN guardian_max_temp_c REAL",
        ]:
            try:
                conn.execute(column_def)
            except sqlite3.OperationalError:
                pass  # column already exists

        # Drop the retired efficiency/performance tuner tables if an older
        # DB still carries them. Child table first (FK), both idempotent.
        for drop_stmt in (
            "DROP TABLE IF EXISTS tuner_points",
            "DROP TABLE IF EXISTS tuner_sessions",
        ):
            try:
                conn.execute(drop_stmt)
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()


async def init_db() -> None:
    # Run the schema synchronously on the main thread. On some
    # filesystems (sandboxes) running setup on a separate thread fails;
    # on the user's Mac it works normally either way.
    _init_db_sync()


# ---------- Miners ----------

async def list_miners(only_enabled: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM miners"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name COLLATE NOCASE"
    async with connect() as conn:
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_miner(miner_id: int) -> dict[str, Any] | None:
    async with connect() as conn:
        async with conn.execute("SELECT * FROM miners WHERE id = ?", (miner_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def find_miner_by_mac(mac: str) -> dict[str, Any] | None:
    async with connect() as conn:
        async with conn.execute("SELECT * FROM miners WHERE mac = ?", (mac,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def find_miner_by_host(host: str) -> dict[str, Any] | None:
    async with connect() as conn:
        async with conn.execute("SELECT * FROM miners WHERE host = ?", (host,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_miner(data: dict[str, Any]) -> int:
    """Insert or update a miner. Match by `mac` if present, otherwise by `host`."""
    ts = now_ts()
    existing = None
    if data.get("mac"):
        existing = await find_miner_by_mac(data["mac"])
    if not existing and data.get("host"):
        existing = await find_miner_by_host(data["host"])

    extra = data.get("extra")
    if isinstance(extra, dict):
        extra = json.dumps(extra)

    if existing:
        # Update the fields we received, keep the rest as-is.
        async with connect() as conn:
            await conn.execute(
                """
                UPDATE miners SET
                  name = COALESCE(?, name),
                  family = COALESCE(?, family),
                  model = COALESCE(?, model),
                  host = COALESCE(?, host),
                  port = COALESCE(?, port),
                  mac = COALESCE(?, mac),
                  enabled = COALESCE(?, enabled),
                  notes = COALESCE(?, notes),
                  fan_threshold_c = COALESCE(?, fan_threshold_c),
                  extra = COALESCE(?, extra),
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    data.get("name"),
                    data.get("family"),
                    data.get("model"),
                    data.get("host"),
                    data.get("port"),
                    data.get("mac"),
                    data.get("enabled"),
                    data.get("notes"),
                    data.get("fan_threshold_c"),
                    extra,
                    ts,
                    existing["id"],
                ),
            )
            await conn.commit()
        return int(existing["id"])

    async with connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO miners
              (name, family, model, host, port, mac, enabled, notes, fan_threshold_c, extra, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("name") or data.get("host") or "miner",
                data["family"],
                data.get("model"),
                data["host"],
                data.get("port"),
                data.get("mac"),
                int(data.get("enabled", 1)),
                data.get("notes"),
                data.get("fan_threshold_c"),
                extra,
                ts,
                ts,
            ),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def update_miner_status(miner_id: int, status: str) -> None:
    async with connect() as conn:
        await conn.execute(
            "UPDATE miners SET last_status = ?, last_seen_ts = ?, updated_at = ? WHERE id = ?",
            (status, now_ts(), now_ts(), miner_id),
        )
        await conn.commit()


async def delete_miner(miner_id: int) -> None:
    async with connect() as conn:
        await conn.execute("DELETE FROM miners WHERE id = ?", (miner_id,))
        await conn.commit()


# ---------- Metrics ----------

async def insert_metric(miner_id: int, ts: int, sample: dict[str, Any]) -> None:
    raw = json.dumps(sample.get("raw")) if sample.get("raw") is not None else None
    async with connect() as conn:
        await conn.execute(
            """
            INSERT INTO metrics
              (miner_id, ts, hashrate_ths, power_w, temp_chip_c, temp_vr_c,
               fan_rpm, fan_pct, frequency_mhz, voltage_mv, uptime_s,
               accepted, rejected, best_difficulty, pool_url, worker, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                miner_id,
                ts,
                sample.get("hashrate_ths"),
                sample.get("power_w"),
                sample.get("temp_chip_c"),
                sample.get("temp_vr_c"),
                sample.get("fan_rpm"),
                sample.get("fan_pct"),
                sample.get("frequency_mhz"),
                sample.get("voltage_mv"),
                sample.get("uptime_s"),
                sample.get("accepted"),
                sample.get("rejected"),
                sample.get("best_difficulty"),
                sample.get("pool_url"),
                sample.get("worker"),
                raw,
            ),
        )
        await conn.commit()


async def latest_metric(miner_id: int) -> dict[str, Any] | None:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM metrics WHERE miner_id = ? ORDER BY ts DESC LIMIT 1",
            (miner_id,),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


# ---------- Best-share records ----------

# Two scopes are tracked, each materialised as one row in `best_records`:
#  - 'session': best since the last miner reboot we detected (via
#    decreasing uptime). Cleared automatically by `update_best_records`
#    when a reboot is observed.
#  - 'alltime': best ever observed by MinerWatch for this miner.
#    Persists across miner reboots, MinerWatch restarts, and even
#    firmware re-flashes (the truth lives in our DB, not in the miner).

_BEST_SCOPES = ("session", "alltime")


async def update_best_records(
    miner_id: int,
    current_value: float | None,
    uptime_s: int | None,
    ts: int | None = None,
    alltime_hint: float | None = None,
) -> dict[str, Any]:
    """Record a new best-share sample, returning records + change events.

    Behaviour:
      * If ``current_value`` is None or <= 0, no write happens for the
        session row, but the all-time row may still be updated via
        ``alltime_hint``.
      * Session reset detection: if ``uptime_s`` is provided and the
        previously stored ``uptime_at_record`` is greater than it (the
        miner rebooted), the session row is cleared first.
      * The session row is upserted only when the new value is strictly
        greater than the stored one (or no row exists).
      * The all-time row is the strict max of ``current_value``,
        ``alltime_hint`` and the stored value, monotonically increasing.
      * ``alltime_hint`` is intended for firmwares that persist their
        own all-time best (Bitaxe NVS ``bestDiff``). When the all-time
        row gets bumped *only* because of the hint (and not the live
        value), ``events.alltime_seeded`` is set instead of
        ``events.new_alltime``: the caller treats it as a silent
        catch-up, not a freshly-broken record (no push notification).

    Return shape::

        {
          "session": {...} | None,    # row AFTER the update
          "alltime": {...} | None,
          "events": {
            "new_session":     bool,              # session row was written this call
            "new_alltime":     bool,              # all-time row was written, current beat the previous
            "alltime_seeded":  bool,              # all-time was bumped via alltime_hint only (silent)
            "prev_session":    {...} | None,      # session row BEFORE the update
            "prev_alltime":    {...} | None,      # all-time row BEFORE the update
            "session_was_reset": bool,            # uptime drop detected (miner rebooted)
          }
        }

    The function is idempotent and tolerant: a malformed call won't
    raise (other than for a real DB error), it just becomes a no-op.
    """
    ts = int(ts or now_ts())
    out: dict[str, Any] = {
        "session": None,
        "alltime": None,
        "events": {
            "new_session": False,
            "new_alltime": False,
            "alltime_seeded": False,
            "prev_session": None,
            "prev_alltime": None,
            "session_was_reset": False,
        },
    }

    async with connect() as conn:
        # Read current rows up-front
        async with conn.execute(
            "SELECT scope, value, ts, uptime_at_record FROM best_records "
            "WHERE miner_id = ?",
            (miner_id,),
        ) as cur:
            rows = await cur.fetchall()
        for r in rows:
            out[r["scope"]] = {
                "value": float(r["value"]),
                "ts": int(r["ts"]),
                "uptime_at_record": (
                    int(r["uptime_at_record"])
                    if r["uptime_at_record"] is not None
                    else None
                ),
            }
        # Snapshot the BEFORE-state for the events block. We deep-copy
        # via dict() so later mutations to `out["session"]` don't bleed
        # back into prev_session.
        out["events"]["prev_session"] = (
            dict(out["session"]) if out["session"] else None
        )
        out["events"]["prev_alltime"] = (
            dict(out["alltime"]) if out["alltime"] else None
        )

        # Session reset: live uptime is strictly less than the stored
        # one => miner rebooted between our two polls.
        if (
            uptime_s is not None
            and out["session"] is not None
            and out["session"]["uptime_at_record"] is not None
            and uptime_s < out["session"]["uptime_at_record"]
        ):
            await conn.execute(
                "DELETE FROM best_records WHERE miner_id = ? AND scope = 'session'",
                (miner_id,),
            )
            out["session"] = None
            out["events"]["session_was_reset"] = True

        # Normalise inputs: treat None / non-positive as "no signal"
        cv = float(current_value) if (current_value is not None and current_value > 0) else None
        hint = float(alltime_hint) if (alltime_hint is not None and alltime_hint > 0) else None

        # ---- Session row: only the live current value can move it ----
        if cv is not None:
            existing = out["session"]
            if existing is None or cv > existing["value"]:
                await conn.execute(
                    """
                    INSERT INTO best_records (miner_id, scope, value, ts, uptime_at_record)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(miner_id, scope) DO UPDATE SET
                      value = excluded.value,
                      ts = excluded.ts,
                      uptime_at_record = excluded.uptime_at_record
                    """,
                    (miner_id, "session", cv, ts, uptime_s),
                )
                out["session"] = {"value": cv, "ts": ts, "uptime_at_record": uptime_s}
                out["events"]["new_session"] = True

        # ---- All-time row: the strict max of (current, hint, stored) ----
        # Two distinct upgrade paths:
        #   - "new_alltime"     : the *live* current value broke the record. This is what triggers the push.
        #   - "alltime_seeded"  : the firmware-persisted hint is ahead of our stored value but the live
        #                         current is not. We silently bump the row to catch up (no push).
        existing_at = out["alltime"]
        existing_at_value = existing_at["value"] if existing_at else None

        # Path A: live value beats both stored and hint → real new record
        if cv is not None and (existing_at_value is None or cv > existing_at_value) \
                and (hint is None or cv >= hint):
            await conn.execute(
                """
                INSERT INTO best_records (miner_id, scope, value, ts, uptime_at_record)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(miner_id, scope) DO UPDATE SET
                  value = excluded.value,
                  ts = excluded.ts,
                  uptime_at_record = excluded.uptime_at_record
                """,
                (miner_id, "alltime", cv, ts, uptime_s),
            )
            out["alltime"] = {"value": cv, "ts": ts, "uptime_at_record": uptime_s}
            out["events"]["new_alltime"] = True

        # Path B: the hint is ahead of what we have stored (and was not
        # already covered by Path A). Silently catch up.
        elif hint is not None and (existing_at_value is None or hint > existing_at_value):
            await conn.execute(
                """
                INSERT INTO best_records (miner_id, scope, value, ts, uptime_at_record)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(miner_id, scope) DO UPDATE SET
                  value = excluded.value,
                  ts = excluded.ts,
                  uptime_at_record = excluded.uptime_at_record
                """,
                # We don't have a real ts for when the firmware found
                # the hinted share — store "now" so the UI shows
                # "seeded just now" rather than 1970.
                (miner_id, "alltime", hint, ts, uptime_s),
            )
            out["alltime"] = {"value": hint, "ts": ts, "uptime_at_record": uptime_s}
            out["events"]["alltime_seeded"] = True

        await conn.commit()
    return out


async def get_miner_best_records(miner_id: int) -> dict[str, dict[str, Any] | None]:
    """Return ``{"session": {...} | None, "alltime": {...} | None}`` for a miner."""
    out: dict[str, dict[str, Any] | None] = {"session": None, "alltime": None}
    async with connect() as conn:
        async with conn.execute(
            "SELECT scope, value, ts, uptime_at_record FROM best_records "
            "WHERE miner_id = ?",
            (miner_id,),
        ) as cur:
            rows = await cur.fetchall()
    for r in rows:
        if r["scope"] not in _BEST_SCOPES:
            continue
        out[r["scope"]] = {
            "value": float(r["value"]),
            "ts": int(r["ts"]),
            "uptime_at_record": (
                int(r["uptime_at_record"])
                if r["uptime_at_record"] is not None
                else None
            ),
        }
    return out


async def get_fleet_best_records() -> dict[str, dict[str, Any] | None]:
    """Return the top record per scope across all enabled miners.

    Output shape:
        {
          "session": {"miner_id": int, "miner_name": str, "value": float, "ts": int} | None,
          "alltime": {...} | None,
        }

    Only enabled miners participate (a removed/disabled miner's old record
    is not "the fleet's best" anymore — it's a relic). Disabled rows are
    still kept in DB so re-enabling a miner restores its history.
    """
    out: dict[str, dict[str, Any] | None] = {"session": None, "alltime": None}
    sql = """
    SELECT b.scope, b.value, b.ts, m.id AS miner_id, m.name AS miner_name
    FROM best_records b
    JOIN miners m ON m.id = b.miner_id
    WHERE m.enabled = 1
    ORDER BY b.scope, b.value DESC
    """
    async with connect() as conn:
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
    seen: set[str] = set()
    for r in rows:
        scope = r["scope"]
        if scope in seen or scope not in _BEST_SCOPES:
            continue
        seen.add(scope)
        out[scope] = {
            "miner_id": int(r["miner_id"]),
            "miner_name": r["miner_name"],
            "value": float(r["value"]),
            "ts": int(r["ts"]),
        }
    return out


async def get_fleet_best_records_ranked(
    scope: str = "alltime",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Ritorna la classifica dei miglior best-record per scope.

    Una riga per miner (lo schema `best_records` ha PK su (miner_id, scope)),
    ordinata per difficoltà discendente. Pensato per la leaderboard
    "Top best shares" nella dashboard. ``scope`` può essere 'alltime' o
    'session'; valori sconosciuti tornano lista vuota. Solo i miner
    ``enabled = 1`` partecipano.

    Output: list[ {miner_id, miner_name, family, value, ts} ].
    """
    if scope not in _BEST_SCOPES:
        return []
    limit = max(1, min(int(limit), 100))
    sql = """
    SELECT b.value, b.ts,
           m.id AS miner_id, m.name AS miner_name, m.family AS family
    FROM best_records b
    JOIN miners m ON m.id = b.miner_id
    WHERE m.enabled = 1 AND b.scope = ?
    ORDER BY b.value DESC
    LIMIT ?
    """
    out: list[dict[str, Any]] = []
    async with connect() as conn:
        async with conn.execute(sql, (scope, limit)) as cur:
            rows = await cur.fetchall()
    for r in rows:
        out.append(
            {
                "miner_id": int(r["miner_id"]),
                "miner_name": r["miner_name"],
                "family": r["family"],
                "value": float(r["value"]),
                "ts": int(r["ts"]),
            }
        )
    return out


# Columns returned by metrics_range across all tiers. Picked so the
# frontend (miner.js) and any downstream consumer can read the same
# shape regardless of which tier served the query. NOTE: the raw JSON
# payload column is intentionally excluded — it's huge and only the
# /api/miners/{id}/raw endpoint needs it (which uses latest_metric).
_METRICS_RANGE_COLS = (
    "ts",
    "hashrate_ths",
    "power_w",
    "temp_chip_c",
    "temp_vr_c",
    "fan_rpm",
    "fan_pct",
    "frequency_mhz",
    "voltage_mv",
    "uptime_s",
    "accepted",
    "rejected",
    "best_difficulty",
)


def _pick_metrics_tier(from_ts: int, to_ts: int) -> str:
    """Choose the storage tier for a metrics query based on range duration.

    Routing rules (range = to_ts - from_ts):
      <= 1 hour       → "metrics"     (raw 5s samples, kept ~48h)
      <= 24 hours     → "metrics_1m"  (1-minute averages, kept ~30d)
      otherwise       → "metrics_1h"  (1-hour averages, kept ~2y)

    The picker only looks at duration, not absolute timestamps. That's
    fine because the UI selectors are always "last N", so a long range
    automatically reaches into older data, where the rollup tier is the
    only place the data still lives.
    """
    span = max(0, int(to_ts) - int(from_ts))
    if span <= 3600:
        return "metrics"
    if span <= 86400:
        return "metrics_1m"
    return "metrics_1h"


async def metrics_range(
    miner_id: int,
    from_ts: int,
    to_ts: int,
) -> tuple[list[dict[str, Any]], str]:
    """Return time-series points for a miner over a time range.

    Picks a storage tier automatically (see ``_pick_metrics_tier``) and
    normalizes the column shape so the caller doesn't need to know which
    table answered. The second element of the tuple is the tier name,
    useful for clients that want to hint resolution.
    """
    tier = _pick_metrics_tier(from_ts, to_ts)
    cols = ", ".join(_METRICS_RANGE_COLS)
    sql = (
        f"SELECT {cols} FROM {tier} "
        "WHERE miner_id = ? AND ts >= ? AND ts <= ? ORDER BY ts ASC"
    )
    async with connect() as conn:
        async with conn.execute(sql, (miner_id, from_ts, to_ts)) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows], tier


# ---------- Rollups ----------

# Source/target shape shared by the two rollup levels. Order matters:
# it must match the column list in INSERT and SELECT below.
_ROLLUP_COLS = (
    "miner_id",
    "ts",
    "hashrate_ths",
    "power_w",
    "temp_chip_c",
    "temp_chip_max_c",
    "temp_vr_c",
    "fan_rpm",
    "fan_pct",
    "frequency_mhz",
    "voltage_mv",
    "uptime_s",
    "accepted",
    "rejected",
    "best_difficulty",
    "sample_count",
)


async def rollup_to_1m(now: int | None = None, lookback_seconds: int = 300) -> int:
    """Aggregate the last few minutes of `metrics` into `metrics_1m`.

    Idempotent thanks to ``INSERT OR REPLACE`` keyed on
    ``(miner_id, ts)``. We re-aggregate a sliding ``lookback_seconds``
    window ending at the start of the *current* minute, so the latest
    incomplete bucket is never written (it would otherwise be overwritten
    again on the next call with a different value).

    Returns the number of bucket rows written.
    """
    n = int(now or now_ts())
    bucket = 60
    end = (n // bucket) * bucket  # exclusive: don't include current minute
    start = end - max(bucket, int(lookback_seconds))

    cols = ", ".join(_ROLLUP_COLS)
    sql = f"""
    INSERT OR REPLACE INTO metrics_1m ({cols})
    SELECT
        miner_id,
        (ts / {bucket}) * {bucket} AS bucket_ts,
        AVG(hashrate_ths),
        AVG(power_w),
        AVG(temp_chip_c),
        MAX(temp_chip_c),
        AVG(temp_vr_c),
        CAST(AVG(fan_rpm) AS INTEGER),
        AVG(fan_pct),
        AVG(frequency_mhz),
        AVG(voltage_mv),
        MAX(uptime_s),
        MAX(accepted),
        MAX(rejected),
        MAX(best_difficulty),
        COUNT(*)
    FROM metrics
    WHERE ts >= ? AND ts < ?
    GROUP BY miner_id, bucket_ts
    """
    async with connect() as conn:
        cur = await conn.execute(sql, (start, end))
        await conn.commit()
        return cur.rowcount or 0


async def rollup_to_1h(now: int | None = None, lookback_seconds: int = 7200) -> int:
    """Aggregate the last few hours of `metrics_1m` into `metrics_1h`.

    Same idempotency strategy as ``rollup_to_1m``: re-aggregate a sliding
    ``lookback_seconds`` window of *closed* hour buckets via
    ``INSERT OR REPLACE``. Aggregating from the 1m tier (rather than raw)
    means we keep working even after raw metrics have been pruned.
    """
    n = int(now or now_ts())
    bucket = 3600
    end = (n // bucket) * bucket
    start = end - max(bucket, int(lookback_seconds))

    cols = ", ".join(_ROLLUP_COLS)
    # NOTE: averaging averages here. With evenly-spaced buckets this is
    # numerically very close to averaging the raw samples; in our case
    # polling is uniform so the bias is negligible. If we ever need
    # exact AVG-of-raw, we'd switch to a weighted mean using
    # ``sample_count``.
    sql = f"""
    INSERT OR REPLACE INTO metrics_1h ({cols})
    SELECT
        miner_id,
        (ts / {bucket}) * {bucket} AS bucket_ts,
        AVG(hashrate_ths),
        AVG(power_w),
        AVG(temp_chip_c),
        MAX(temp_chip_max_c),
        AVG(temp_vr_c),
        CAST(AVG(fan_rpm) AS INTEGER),
        AVG(fan_pct),
        AVG(frequency_mhz),
        AVG(voltage_mv),
        MAX(uptime_s),
        MAX(accepted),
        MAX(rejected),
        MAX(best_difficulty),
        SUM(sample_count)
    FROM metrics_1m
    WHERE ts >= ? AND ts < ?
    GROUP BY miner_id, bucket_ts
    """
    async with connect() as conn:
        cur = await conn.execute(sql, (start, end))
        await conn.commit()
        return cur.rowcount or 0


# ---------- Tiered retention ----------

async def cleanup_tiered(
    retention_raw_hours: int,
    retention_1m_days: int,
    retention_1h_days: int,
) -> dict[str, int]:
    """Apply per-tier retention. Returns rows deleted per tier.

    Each tier is independent: shrinking ``retention_raw_hours`` doesn't
    affect ``metrics_1m`` because the rollup has already produced the
    aggregated rows. Order doesn't matter for correctness.
    """
    n = now_ts()
    deleted = {"metrics": 0, "metrics_1m": 0, "metrics_1h": 0}
    plans = [
        ("metrics",    n - max(1, int(retention_raw_hours)) * 3600),
        ("metrics_1m", n - max(1, int(retention_1m_days))  * 86400),
        ("metrics_1h", n - max(1, int(retention_1h_days))  * 86400),
    ]
    async with connect() as conn:
        for table, cutoff in plans:
            cur = await conn.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            deleted[table] = cur.rowcount or 0
        await conn.commit()
    return deleted


async def cleanup_old_metrics(retention_days: int) -> int:
    """Backward-compat shim. Old code paths called this with a single
    ``retention_days``; we now express that as the 1m-tier retention.
    Raw and 1h retention are filled in with their conventional defaults
    so a caller that only knows the legacy knob still does sensible work.
    """
    result = await cleanup_tiered(
        retention_raw_hours=48,
        retention_1m_days=int(retention_days),
        retention_1h_days=max(int(retention_days), 730),
    )
    return sum(result.values())


# ---------- One-shot tier migration ----------

async def is_tier_migration_done() -> bool:
    val = await get_setting("_tier_migration_done", "0")
    return (val or "0").strip() not in ("0", "", "false", "False")


async def run_tier_migration(
    retention_raw_hours: int = 48,
    vacuum: bool = True,
) -> dict[str, Any]:
    """Backfill `metrics_1m` and `metrics_1h` from existing data, then
    trim `metrics` to the new raw retention, and (optionally) VACUUM.

    Designed to be safe to invoke multiple times: it short-circuits if
    ``_tier_migration_done`` is already set. The caller (startup) is
    responsible for that guard, but we also re-check here.
    """
    if await is_tier_migration_done():
        return {"skipped": True}

    # 1. Full backfill: aggregate ALL existing rows in `metrics` into
    # 1-minute buckets. The lookback window is the entire span of the
    # table, which on a fresh upgrade is at most a few weeks of data.
    async with connect() as conn:
        async with conn.execute(
            "SELECT MIN(ts) AS mn, MAX(ts) AS mx FROM metrics"
        ) as cur:
            row = await cur.fetchone()
    span = {"min_ts": row["mn"], "max_ts": row["mx"]} if row else {"min_ts": None, "max_ts": None}

    rolled_1m = 0
    rolled_1h = 0
    if span["min_ts"] is not None:
        # rollup_to_1m takes a sliding window; for full backfill we
        # extend the lookback to cover the full data span.
        full_lookback = max(60, int(span["max_ts"]) - int(span["min_ts"]) + 60)
        rolled_1m = await rollup_to_1m(now=int(span["max_ts"]) + 60, lookback_seconds=full_lookback)
        # Now roll up to 1h from the freshly populated 1m tier.
        rolled_1h = await rollup_to_1h(now=int(span["max_ts"]) + 3600, lookback_seconds=full_lookback)

    # 2. Trim raw `metrics` to the new retention.
    cutoff = now_ts() - max(1, int(retention_raw_hours)) * 3600
    async with connect() as conn:
        cur = await conn.execute("DELETE FROM metrics WHERE ts < ?", (cutoff,))
        await conn.commit()
        deleted_raw = cur.rowcount or 0

    # 3. VACUUM to actually reclaim disk pages. SQLite leaves freed
    # pages as holes inside the file otherwise; users expect to see the
    # file shrink. VACUUM rewrites the DB and briefly needs ~2x disk.
    #
    # NOTE: VACUUM requires "no SQL statements in progress" on the
    # connection, and our async `connect()` helper sets PRAGMAs whose
    # cursors are not explicitly closed — that causes SQLite to refuse
    # the VACUUM. We sidestep the issue by opening a fresh synchronous
    # sqlite3 connection just for VACUUM (same trick `_init_db_sync`
    # already uses for schema setup).
    vacuumed = False
    if vacuum:
        try:
            import sqlite3  # local import: setup-only path
            v_conn = sqlite3.connect(str(db_path()), isolation_level=None)
            try:
                v_conn.execute("VACUUM")
                vacuumed = True
            finally:
                v_conn.close()
        except Exception:  # noqa: BLE001
            # Some filesystems can fail VACUUM (locked / out-of-space).
            # Don't make migration fatal — the rollup data is already
            # saved and the regular cleanup will keep the DB in check
            # going forward, just without the immediate file shrink.
            vacuumed = False

    await set_setting("_tier_migration_done", "1")
    return {
        "skipped": False,
        "data_span": span,
        "rolled_1m": rolled_1m,
        "rolled_1h": rolled_1h,
        "raw_deleted": deleted_raw,
        "vacuumed": vacuumed,
    }


async def fleet_hashrate_buckets(
    from_ts: int,
    to_ts: int,
    bucket_seconds: int = 60,
) -> tuple[list[dict[str, Any]], str]:
    """Total fleet hashrate aggregated by time buckets.

    Two-step strategy:
      1. For each ``(miner_id, bucket)`` compute the average of
         ``hashrate_ths`` over the samples falling in that bucket. This
         way miners that poll faster are not weighted more.
      2. For each bucket sum across miners. The result is the
         "bucket-average" total hashrate of the fleet.

    The value stored in ``hashrate_ths`` is already smoothed by the
    firmware (Bitaxe: instantaneous; Avalon: ``MHS 5m`` → ``MHS 1m`` →
    ``MHS av``; Braiins: ``GHS 1m``). With ``bucket_seconds=60`` we
    therefore get a "total hashrate, 1-minute average" chart.

    Storage tier is picked automatically from the range duration (same
    routing as ``metrics_range``): short windows read raw 5s samples,
    medium windows read 1m rollups, long windows read 1h rollups. The
    chosen tier is returned alongside the rows so callers can label
    the chart.
    """
    bucket_seconds = max(1, int(bucket_seconds))
    tier = _pick_metrics_tier(from_ts, to_ts)
    sql = f"""
    SELECT bucket_ts, SUM(avg_ths) AS total_ths
    FROM (
        SELECT
            (ts / ?) * ? AS bucket_ts,
            miner_id,
            AVG(hashrate_ths) AS avg_ths
        FROM {tier}
        WHERE ts >= ? AND ts <= ? AND hashrate_ths IS NOT NULL
        GROUP BY bucket_ts, miner_id
    )
    GROUP BY bucket_ts
    ORDER BY bucket_ts ASC
    """
    async with connect() as conn:
        async with conn.execute(
            sql, (bucket_seconds, bucket_seconds, from_ts, to_ts)
        ) as cur:
            rows = await cur.fetchall()
    points = [
        {"bucket_ts": int(r["bucket_ts"]), "total_ths": float(r["total_ths"] or 0)}
        for r in rows
    ]
    return points, tier


# ---------- Alerts ----------

async def insert_alert(
    miner_id: int | None,
    severity: str,
    code: str,
    message: str,
) -> int:
    async with connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO alerts (miner_id, ts, severity, code, message)
            VALUES (?, ?, ?, ?, ?)
            """,
            (miner_id, now_ts(), severity, code, message),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def list_alerts(limit: int = 200, only_unack: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM alerts"
    if only_unack:
        sql += " WHERE acknowledged = 0"
    sql += " ORDER BY ts DESC LIMIT ?"
    async with connect() as conn:
        async with conn.execute(sql, (limit,)) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def ack_alert(alert_id: int) -> None:
    async with connect() as conn:
        await conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
        await conn.commit()


# ---------- Settings ----------

async def get_setting(key: str, default: str | None = None) -> str | None:
    async with connect() as conn:
        async with conn.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    async with connect() as conn:
        await conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await conn.commit()


async def all_settings() -> dict[str, str]:
    async with connect() as conn:
        async with conn.execute("SELECT key, value FROM settings") as cur:
            rows = await cur.fetchall()
    return {r["key"]: r["value"] for r in rows}


# ---------- Push subscriptions ----------

async def add_push_sub(endpoint: str, p256dh: str, auth_key: str, user_agent: str | None) -> int:
    async with connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(endpoint) DO UPDATE SET
              p256dh = excluded.p256dh,
              auth = excluded.auth,
              user_agent = excluded.user_agent
            """,
            (endpoint, p256dh, auth_key, user_agent, now_ts()),
        )
        await conn.commit()
        return int(cur.lastrowid or 0)


async def list_push_subs() -> list[dict[str, Any]]:
    async with connect() as conn:
        async with conn.execute("SELECT * FROM push_subscriptions") as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def remove_push_sub(endpoint: str) -> None:
    async with connect() as conn:
        await conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        await conn.commit()


async def purge_push_subs() -> int:
    """Cancella tutte le push subscription dal DB. Ritorna quante ne ha eliminate."""
    async with connect() as conn:
        cur = await conn.execute("DELETE FROM push_subscriptions")
        await conn.commit()
        return cur.rowcount or 0


# ---------- Fan / auto control ----------

async def set_fan_config(
    miner_id: int,
    fan_mode: str | None = None,
    auto_target_c: float | None = None,
    fan_min_override: int | None = None,
    fan_max_override: int | None = None,
    fan_threshold_c: float | None = None,
) -> None:
    """Update the fan-control settings for a miner.

    All fields are optional: pass only the ones you want to change,
    the others are left untouched (COALESCE).
    """
    if fan_mode is not None and fan_mode not in ("manual", "firmware", "minerwatch"):
        raise ValueError(f"invalid fan_mode: {fan_mode!r}")
    async with connect() as conn:
        await conn.execute(
            """
            UPDATE miners SET
              fan_mode = COALESCE(?, fan_mode),
              auto_target_c = COALESCE(?, auto_target_c),
              fan_min_override = COALESCE(?, fan_min_override),
              fan_max_override = COALESCE(?, fan_max_override),
              fan_threshold_c = COALESCE(?, fan_threshold_c),
              updated_at = ?
            WHERE id = ?
            """,
            (
                fan_mode,
                auto_target_c,
                fan_min_override,
                fan_max_override,
                fan_threshold_c,
                now_ts(),
                miner_id,
            ),
        )
        await conn.commit()


# ---------- Block finds (solo-mining wins) ----------
# Persisting these is the whole point of the feature: a home solo miner
# wants to see "I once mined block N" on their dashboard for years.

async def insert_block_find(
    miner_id: int | None,
    miner_name: str,
    share_difficulty: float,
    network_difficulty: float,
    ts: int | None = None,
    block_height: int | None = None,
) -> int:
    """Record a block-found event. Returns the new row id.

    ``miner_id`` is nullable on the FK side so a miner deletion doesn't
    erase the historical win — the ``miner_name`` snapshot keeps the
    record human-readable forever.
    """
    when = ts if ts is not None else now_ts()
    async with connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO block_finds
              (miner_id, miner_name, ts, share_difficulty,
               network_difficulty, block_height)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (miner_id, miner_name, when, float(share_difficulty),
             float(network_difficulty), block_height),
        )
        await conn.commit()
        return cur.lastrowid or 0


async def list_block_finds(limit: int = 50) -> list[dict[str, Any]]:
    """Return the most recent block-found events, newest first."""
    async with connect() as conn:
        async with conn.execute(
            """
            SELECT id, miner_id, miner_name, ts, share_difficulty,
                   network_difficulty, block_height
            FROM block_finds
            ORDER BY ts DESC
            LIMIT ?
            """,
            (int(limit),),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def last_block_find_share_value(miner_id: int) -> float | None:
    """Return the highest share difficulty already recorded as a block
    find for this miner, or ``None`` if there is none.

    The poller uses it as anti-duplication: if the current share is at or
    below the previous block-find value, we don't fire again. A new
    block-find must strictly exceed the last one to count.
    """
    async with connect() as conn:
        async with conn.execute(
            "SELECT MAX(share_difficulty) AS v FROM block_finds WHERE miner_id = ?",
            (miner_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or row["v"] is None:
        return None
    return float(row["v"])


# ---------- Notable shares (near-block Hall of Fame) ----------

async def insert_notable_share(
    miner_id: int,
    ts: int,
    share_difficulty: float,
    pool_target: float | None = None,
    keep_per_miner: int = 500,
) -> int:
    """Record a notable share and return its row id.

    Fed by the live log streamer. After inserting we prune this miner's
    rows down to the ``keep_per_miner`` highest difficulties, so the
    table can't grow without bound on a long-running instance.

    ``accepted`` is left NULL here; the streamer back-fills it with
    :func:`set_notable_share_accepted` when the pool verdict arrives.
    """
    async with connect() as conn:
        cur = await conn.execute(
            """
            INSERT INTO notable_shares
              (miner_id, ts, share_difficulty, pool_target, accepted)
            VALUES (?, ?, ?, ?, NULL)
            """,
            (int(miner_id), int(ts), float(share_difficulty),
             float(pool_target) if pool_target is not None else None),
        )
        rowid = cur.lastrowid or 0
        # Keep only the top-N by difficulty for this miner.
        await conn.execute(
            """
            DELETE FROM notable_shares
            WHERE miner_id = ?
              AND id NOT IN (
                SELECT id FROM notable_shares
                WHERE miner_id = ?
                ORDER BY share_difficulty DESC, ts DESC
                LIMIT ?
              )
            """,
            (int(miner_id), int(miner_id), int(keep_per_miner)),
        )
        await conn.commit()
        return rowid


async def set_notable_share_accepted(rowid: int, accepted: bool) -> None:
    """Back-fill the pool's accept/reject verdict on a notable share."""
    async with connect() as conn:
        await conn.execute(
            "UPDATE notable_shares SET accepted = ? WHERE id = ?",
            (1 if accepted else 0, int(rowid)),
        )
        await conn.commit()


async def list_notable_shares(miner_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Top notable shares for a miner, highest difficulty first."""
    async with connect() as conn:
        async with conn.execute(
            """
            SELECT id, miner_id, ts, share_difficulty, pool_target, accepted
            FROM notable_shares
            WHERE miner_id = ?
            ORDER BY share_difficulty DESC, ts DESC
            LIMIT ?
            """,
            (int(miner_id), int(limit)),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---------- Guardian (runtime frequency governor) ----------
# Per-miner knobs for the Guardian live on the `miners` row (so they ride
# along with get_miner/list_miners' SELECT *). Only the writer needs a
# dedicated accessor; reads come through the normal miner record. See
# backend/guardian.py and docs/guardian-design.md.


async def set_guardian_config(
    miner_id: int,
    enabled: bool | None = None,
    max_freq_mhz: int | None = None,
    freq_floor_mhz: int | None = None,
    temp_source: str | None = None,
    max_temp_c: float | None = None,
) -> None:
    """Update the Guardian settings for a miner.

    All fields are optional: pass only the ones you want to change, the
    others are left untouched (COALESCE). ``enabled`` is stored as 0/1.
    ``temp_source`` is "vr" | "chip" (which sensor governs frequency) and
    ``max_temp_c`` is the per-miner high threshold (the recovery point is
    derived from it at decision time).

    Note: COALESCE means a value can't be reset back to NULL here (mirrors
    set_fan_config). That's intentional — clearing the ceiling/floor isn't a
    supported operation; the caller sets a concrete value or leaves it.
    """
    enabled_int = None if enabled is None else (1 if enabled else 0)
    source = None if temp_source is None else str(temp_source).lower()
    async with connect() as conn:
        await conn.execute(
            """
            UPDATE miners SET
              guardian_enabled = COALESCE(?, guardian_enabled),
              guardian_max_freq_mhz = COALESCE(?, guardian_max_freq_mhz),
              guardian_freq_floor_mhz = COALESCE(?, guardian_freq_floor_mhz),
              guardian_temp_source = COALESCE(?, guardian_temp_source),
              guardian_max_temp_c = COALESCE(?, guardian_max_temp_c),
              updated_at = ?
            WHERE id = ?
            """,
            (
                enabled_int,
                max_freq_mhz,
                freq_floor_mhz,
                source,
                max_temp_c,
                now_ts(),
                miner_id,
            ),
        )
        await conn.commit()


# ---------- Donate hashrate ----------
#
# A "donation" is one user action that may cover several miners. Each
# miner gets a donation_miners row holding the JSON snapshot of its pool
# config (prev_pool) so we can restore it on revert / boot catch-up.
# A child is "still in flight" while its status is one of these:
_DONATION_ACTIVE_STATES = ("active", "unreachable")


async def create_donation(
    ends_ts: int, worker_name: str, note: str | None = None
) -> int:
    """Create a donation row (status 'active'). Returns its id."""
    async with connect() as conn:
        cur = await conn.execute(
            "INSERT INTO donations (created_ts, ends_ts, status, worker_name, note) "
            "VALUES (?, ?, 'active', ?, ?)",
            (now_ts(), int(ends_ts), worker_name, note),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def add_donation_miner(
    donation_id: int,
    miner_id: int,
    prev_pool: str,
    status: str = "active",
    applied_ts: int | None = None,
    last_error: str | None = None,
) -> int:
    """Attach a miner (with its pre-donation pool snapshot) to a donation."""
    async with connect() as conn:
        cur = await conn.execute(
            "INSERT INTO donation_miners "
            "(donation_id, miner_id, status, prev_pool, applied_ts, last_error) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (donation_id, miner_id, status, prev_pool, applied_ts, last_error),
        )
        await conn.commit()
        return int(cur.lastrowid)


async def active_donation_miner_ids() -> set[int]:
    """Miner ids currently in an in-flight donation — used to refuse
    donating the same miner twice at once."""
    async with connect() as conn:
        async with conn.execute(
            "SELECT DISTINCT miner_id FROM donation_miners "
            "WHERE status IN ('active', 'unreachable')"
        ) as cur:
            rows = await cur.fetchall()
    return {int(r["miner_id"]) for r in rows}


async def list_donation_miners(active_only: bool = True) -> list[dict[str, Any]]:
    """Flattened view of donation miners joined with donation + miner info.
    Powers the active-donations table in the UI."""
    sql = """
        SELECT dm.id, dm.donation_id, dm.miner_id, dm.status, dm.prev_pool,
               dm.applied_ts, dm.reverted_ts, dm.last_error,
               d.ends_ts, d.created_ts, d.worker_name,
               d.status AS donation_status,
               m.name AS miner_name, m.family AS miner_family, m.host AS miner_host
        FROM donation_miners dm
        JOIN donations d ON d.id = dm.donation_id
        LEFT JOIN miners m ON m.id = dm.miner_id
    """
    if active_only:
        sql += " WHERE dm.status IN ('active', 'unreachable')"
    sql += " ORDER BY d.ends_ts ASC, dm.id ASC"
    async with connect() as conn:
        async with conn.execute(sql) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def donation_miners_due(now: int) -> list[dict[str, Any]]:
    """In-flight children whose donation window has elapsed (ready to
    auto-revert). Also returns those still flagged unreachable so the
    controller keeps retrying them."""
    async with connect() as conn:
        async with conn.execute(
            """
            SELECT dm.*, d.ends_ts, d.worker_name
            FROM donation_miners dm
            JOIN donations d ON d.id = dm.donation_id
            WHERE dm.status IN ('active', 'unreachable') AND d.ends_ts <= ?
            ORDER BY dm.id ASC
            """,
            (int(now),),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_donation_miner(dm_id: int) -> dict[str, Any] | None:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM donation_miners WHERE id = ?", (dm_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def donation_miners_for(donation_id: int) -> list[dict[str, Any]]:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM donation_miners WHERE donation_id = ? ORDER BY id ASC",
            (donation_id,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_donation_miner(
    dm_id: int,
    *,
    status: str | None = None,
    applied_ts: int | None = None,
    reverted_ts: int | None = None,
    last_error: str | None = None,
) -> None:
    """Update a donation_miner. COALESCE means only the fields passed are
    changed (you can't reset a column back to NULL here)."""
    async with connect() as conn:
        await conn.execute(
            """
            UPDATE donation_miners SET
              status      = COALESCE(?, status),
              applied_ts  = COALESCE(?, applied_ts),
              reverted_ts = COALESCE(?, reverted_ts),
              last_error  = COALESCE(?, last_error)
            WHERE id = ?
            """,
            (status, applied_ts, reverted_ts, last_error, dm_id),
        )
        await conn.commit()


async def recompute_donation_status(donation_id: int) -> str:
    """Roll the donation's status up from its children and persist it.
    active (any child still in flight) | completed (all reverted) |
    partial_error (no child in flight but at least one errored)."""
    children = await donation_miners_for(donation_id)
    statuses = {c["status"] for c in children}
    if statuses & set(_DONATION_ACTIVE_STATES):
        new = "active"
    elif "error" in statuses:
        new = "partial_error"
    else:
        new = "completed"
    async with connect() as conn:
        await conn.execute(
            "UPDATE donations SET status = ? WHERE id = ?", (new, donation_id)
        )
        await conn.commit()
    return new


async def get_donation(donation_id: int) -> dict[str, Any] | None:
    async with connect() as conn:
        async with conn.execute(
            "SELECT * FROM donations WHERE id = ?", (donation_id,)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None

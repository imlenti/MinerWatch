# SPDX-License-Identifier: AGPL-3.0-only
"""MinerWatch FastAPI entrypoint.

Run:
    uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload

The ``start.sh`` script in the repo root does exactly this after
setting up the virtualenv.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import asdict
from typing import Any, Dict, Optional

from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db
from . import coin_difficulty
from . import system_info
from .alerts import ensure_vapid_keys, public_key_b64
from .auth import (
    clear_login_failures,
    login_lockout_remaining,
    public_paths,
    record_login_failure,
    require_auth,
)
from .auto_control import auto_fan
from .donations import donation_controller
from .config import FRONTEND_DIR, db_path, get_config, reload_config
from .discovery import discover_and_register, identify_host, scan_network
from .miners import DRIVERS, driver_for_record
from .poller import poller
from .guardian import guardian, GUARDIAN_FAMILIES
from .log_streamer import log_streamer
from .mqtt import mqtt_publisher, test_connection as mqtt_test_connection
from . import updater

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("minerwatch")

app = FastAPI(title="MinerWatch", version=updater.read_version())

# CORS — accept any origin that lives on the local network (mDNS
# `*.local`, RFC1918 IPv4 ranges, IPv6 link-local/ULA, plus
# localhost/127.0.0.1). We still refuse public origins, so a malicious
# site on the open web can't trick the browser into reading
# MinerWatch's responses just because the user is logged in.
#
# We use `allow_origin_regex` rather than enumerating every possible
# host because MinerWatch is reached from a mix of mDNS hostnames
# (denver.local), raw LAN IPs (192.168.x.y, 10.x, 172.16-31.x), and on
# iOS Bonjour resolution sometimes silently falls back to the IP. With
# a fixed allow-list every device fails on a different morning.
PRIVATE_ORIGIN_REGEX = (
    r"^https?://("
    r"localhost"
    r"|127\.0\.0\.1"
    r"|\[::1\]"
    r"|[a-zA-Z0-9-]+\.local"
    r"|10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    r"|192\.168\.\d{1,3}\.\d{1,3}"
    r"|172\.(1[6-9]|2[0-9]|3[01])\.\d{1,3}\.\d{1,3}"
    r"|\[fe80::[0-9a-fA-F:]+(%[0-9a-zA-Z]+)?\]"
    r"|\[fd[0-9a-fA-F]{2}:[0-9a-fA-F:]*\]"
    r")(:\d+)?$"
)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=PRIVATE_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------- Lifecycle ----------

@app.on_event("startup")
async def on_startup() -> None:
    cfg = get_config()
    await db.init_db()

    # Apply any overrides from the settings DB
    overrides = await db.all_settings()
    cfg.apply_overrides(
        {k: v for k, v in overrides.items() if not k.startswith("_")}
    )

    # One-shot tiered-retention migration. Backfills metrics_1m and
    # metrics_1h from existing raw data, trims raw to the configured
    # retention, and VACUUMs to actually shrink the file. The function
    # short-circuits if it has already run.
    if not await db.is_tier_migration_done():
        log.info("Running tiered-retention migration (one-shot)…")
        result = await db.run_tier_migration(
            retention_raw_hours=cfg.storage.retention_raw_hours,
            vacuum=True,
        )
        log.info(
            "Tier migration done: rolled_1m=%s rolled_1h=%s raw_deleted=%s vacuumed=%s",
            result.get("rolled_1m"),
            result.get("rolled_1h"),
            result.get("raw_deleted"),
            result.get("vacuumed"),
        )

    ensure_vapid_keys()

    # Fail-closed sanity check: if auth.enabled is True but the password
    # is empty, every protected request will 401. We don't crash the
    # process (that would create a boot loop and lock the user out
    # without a fix path), but we surface a loud warning in the log so
    # the misconfiguration isn't silent.
    if cfg.auth.enabled and not (cfg.auth.password or "").strip():
        log.warning(
            "auth.enabled=True but auth.password is empty — all protected "
            "requests will be rejected with 401. Either set a password in "
            "/settings, or disable auth in config.yaml / via the DB."
        )

    log.info("Starting MinerWatch — port %s", cfg.server.port)
    await poller.start()
    await auto_fan.start()
    # Runtime frequency governor (Guardian). Slow outer loop; per-miner
    # opt-in. See backend/guardian.py and docs/guardian-design.md.
    await guardian.start()
    # Live per-share streamer for AxeOS miners. Self-disables if the
    # `websockets` lib is missing; only attaches to bitaxe-family miners.
    await log_streamer.start()
    # Donate-hashrate: first revert anything whose window elapsed while we
    # were down (boot catch-up = crash safety net), then start the loop
    # that auto-reverts on expiry. See backend/donations.py.
    await donation_controller.catch_up_on_boot()
    await donation_controller.start()
    # Optional MQTT publisher (Home Assistant / ESPHome). Self-disables when
    # mqtt.enabled is false or the aiomqtt dependency is missing.
    await mqtt_publisher.start()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    await mqtt_publisher.stop()
    await donation_controller.stop()
    await log_streamer.stop()
    await guardian.stop()
    await auto_fan.stop()
    await poller.stop()


# ---------- Auth middleware ----------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    cfg = get_config()
    path = request.url.path
    if cfg.auth.enabled and not public_paths(path):
        try:
            require_auth(request)
        except HTTPException as exc:
            # For API requests return 401 JSON; for HTML pages do a *real*
            # 302 redirect to /login with the original target as `next=`.
            # Serving login.html inline at the protected URL caused two
            # nasty issues: (1) browsers cached the login form under the
            # protected URL, creating a "click Settings → see login" loop,
            # and (2) the URL bar lied about what the user was looking at.
            if path.startswith("/api/"):
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            target = path
            if request.url.query:
                target = f"{path}?{request.url.query}"
            return RedirectResponse(
                url=f"/login?next={quote(target, safe='')}",
                status_code=302,
            )
    response = await call_next(request)
    # When auth is enabled, the HTML page responses should never be cached
    # by intermediaries or the browser: a cached login or settings page
    # could leak to other users / sessions, and on iOS Safari the HTTP
    # cache is aggressive enough to serve stale content even after the
    # cookie has been set. Static assets keep their own cache policy.
    is_html_page = (
        path in {"/", "/settings", "/analytics", "/live", "/system", "/update", "/login"}
        or path.startswith("/miner/")
    )
    if cfg.auth.enabled and is_html_page:
        response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---------- Frontend (React SPA) ----------
#
# The MinerWatch UI is a single-page React app built from
# `frontend-react/` into `frontend-react/dist/`. FastAPI serves the
# bundle directly:
#
#   - /assets/*       → hashed JS/CSS chunks emitted by Vite
#   - /sw.js          → service worker for Web Push
#   - /favicon.svg    → favicon
#   - everything else → dist/index.html, so React Router can handle
#                       client-side routes (/, /miner/:id, /settings,
#                       /analytics, /system, /login)
#
# The legacy vanilla frontend was retired in P1 session 5; its sources
# are still in git history if you ever need to look back at them.

REACT_DIST = FRONTEND_DIR  # alias for clarity: dist is now the only frontend


def _react_index_response() -> Response:
    """Serve dist/index.html, or a 503 with a setup hint if not built yet.

    The index.html references hashed asset bundles under /assets/<hash>.js.
    iOS Safari and Chrome iOS keep an aggressive HTTP cache that will
    happily serve an old index.html for hours after a new deploy — and
    that old index.html points at /assets/ chunks that no longer exist
    on disk, producing a blank page on iPad/iPhone with no error in the
    UI. We always set Cache-Control: no-store on the HTML shell to make
    sure every page load fetches the current entry point. The hashed
    assets themselves keep their own (long, immutable) cache policy via
    the StaticFiles mount below.
    """
    index = REACT_DIST / "index.html"
    if not index.exists():
        return JSONResponse(
            {
                "detail": (
                    "Frontend not built yet. Run `cd frontend-react && "
                    "npm install && npm run build` on the host, or rebuild "
                    "the Docker image (the Dockerfile builds it for you)."
                )
            },
            status_code=503,
        )
    return FileResponse(
        index,
        media_type="text/html",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


# Make sure the assets dir exists on first boot so the mount below
# doesn't blow up — the actual files arrive when `npm run build` runs.
(REACT_DIST / "assets").mkdir(parents=True, exist_ok=True)


# Vite-emitted JS/CSS chunks. They include a content hash in the
# filename, so we can ask the browser to cache them aggressively: a
# change to the bundle = a new filename = a guaranteed cache miss.
app.mount(
    "/assets",
    StaticFiles(directory=str(REACT_DIST / "assets")),
    name="assets",
)


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> Response:
    """Web Push service worker. Lives at the root so its scope covers /.

    The SW script must never be cached: if we ship a new version, every
    browser needs to pick it up on the next visit so its activate
    handler can purge stale caches and unregister old behaviour.
    """
    sw = REACT_DIST / "sw.js"
    if not sw.exists():
        return Response(status_code=404)
    return FileResponse(
        sw,
        media_type="application/javascript",
        headers={"Cache-Control": "no-store, must-revalidate"},
    )


@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg() -> Response:
    fav = REACT_DIST / "favicon.svg"
    if not fav.exists():
        return Response(status_code=404)
    return FileResponse(fav, media_type="image/svg+xml")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> Response:
    # Some browsers still probe for /favicon.ico even when the HTML
    # declares <link rel="icon" href="/favicon.svg">. Redirect them.
    fav = REACT_DIST / "favicon.svg"
    if not fav.exists():
        return Response(status_code=204)
    return FileResponse(fav, media_type="image/svg+xml")


# Legacy /v2/* paths from the staging period — keep them alive for a
# bit so any bookmark or open tab still works.
@app.get("/v2", include_in_schema=False)
@app.get("/v2/{path:path}", include_in_schema=False)
async def v2_redirect(path: str = "") -> Response:
    target = f"/{path}" if path else "/"
    return RedirectResponse(url=target, status_code=308)


# ---------- API: miners ----------

class MinerCreate(BaseModel):
    """Manual "Add miner" payload.

    Only the address is required: MinerWatch connects to the miner and
    auto-detects everything else (family, port, MAC, model, name) with
    the same fingerprint auto-discovery uses. This is why ``family`` /
    ``port`` / ``name`` are no longer accepted from the client -- a
    user-declared family is exactly what used to mis-save NerdOctaxe /
    NerdQAxe boards as plain ``bitaxe``.
    """

    host: str
    notes: Optional[str] = None


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "version": app.version}


# ---------- API: version & self-update ----------
#
# Three small endpoints power the "Update" page in the SPA:
#
#   GET  /api/version         → installed version + host OS info.
#   GET  /api/update/check    → comparison with the latest GitHub
#                               release. Cached 6h on disk to stay
#                               under the anonymous GitHub rate limit.
#   POST /api/update/install  → download + verify SHA256 + swap files
#                               + schedule an os._exit(1) so the
#                               LaunchAgent / systemd unit relaunches
#                               us. The install endpoint stays behind
#                               the same auth gate as the rest of the
#                               write API (it's destructive); the two
#                               read endpoints are public so the
#                               sidebar badge can render before login.

@app.get("/api/version")
async def api_version() -> dict:
    return {
        "version": updater.read_version(),
        "system": updater.system_summary(),
        # True under Docker/Umbrel. The frontend uses this to swap the
        # "Install" button for `docker compose pull` instructions while still
        # showing whether a newer release exists. Never set on bare-metal.
        "container": updater.in_container(),
    }


@app.get("/api/update/check")
async def api_update_check(force: bool = False) -> dict:
    """Hit GitHub Releases and return the diff against the local VERSION.

    Returns the :class:`updater.UpdateCheckResult` shape directly so
    the frontend can spread it into the UI. On any failure, the
    response still contains ``current`` and ``available: false`` plus
    a short ``error`` code (``no_releases``, ``rate_limited``,
    ``network_error``, ``github_http_<code>``) so the UI can render a
    sensible message instead of throwing.
    """
    result = await updater.check_for_update(force=force)
    # asdict avoids leaking the dataclass identity to JSON consumers
    # and gives the SPA a plain object to consume.
    from dataclasses import asdict as _asdict
    return _asdict(result)


@app.post("/api/update/install")
async def api_update_install() -> dict:
    """Kick off the self-update.

    Returns immediately with ``{"status": "restarting", ...}`` — the
    actual process exit is deferred ~1.5 s so this response can flush
    to the frontend, which then polls ``/api/version`` until the new
    version answers (signalling that the relaunched process is up).

    Under Docker/Umbrel we refuse with 409: the self-update would swap files
    into the container's ephemeral layer and ``os._exit`` would just have the
    orchestrator recreate the container from the unchanged image, silently
    reverting the "update". The read-only check endpoint stays available so
    the UI can still tell the user a newer release exists and how to pull it.
    """
    if updater.in_container():
        raise HTTPException(
            status_code=409,
            detail=(
                "In-app updates are disabled under Docker/Umbrel because the "
                "container image is immutable. Update with "
                "`docker compose pull && docker compose up -d` (or bump the "
                "app version in the Umbrel App Store)."
            ),
        )
    try:
        return await updater.install_update()
    except updater.UpdateError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/miners")
async def api_list_miners() -> dict:
    miners = await db.list_miners()
    out = []
    for m in miners:
        latest = await db.latest_metric(m["id"])
        sample = poller.last_results.get(m["id"])
        out.append(
            {
                **m,
                "last_metric": latest,
                "live_online": bool(sample.online) if sample else None,
                "live_error": sample.error if sample else None,
            }
        )
    return {"miners": out}


@app.get("/api/pools")
async def api_list_pools() -> dict:
    """Flat view of every (miner, pool slot) currently configured.

    The shape is intentionally a single denormalised list (rather than
    a per-miner nested structure) because the Pools page in the SPA is
    a sortable / filterable table: one row per pool slot makes that a
    one-liner to render, with the alternative — grouping by URL on the
    server — left as a client-side toggle.

    Reads from :attr:`poller.last_results` (the live in-memory snapshot
    map keyed by miner id), so this endpoint costs no DB hits and
    refreshes at the cadence of the poller.

    For miners we haven't polled yet (e.g. just-added, or the poller
    hasn't ticked since startup), we synthesise a placeholder row from
    the DB record so the user still sees the miner in the table —
    showing one row per known miner is friendlier than an empty page
    that looks broken until the first poll completes.
    """
    miners = await db.list_miners()
    rows: list[dict[str, Any]] = []
    for m in miners:
        sample = poller.last_results.get(m["id"])
        live_online = bool(sample.online) if sample else None
        live_error = sample.error if sample else None
        miner_meta = {
            "miner_id": m["id"],
            "miner_name": m.get("name") or m.get("host"),
            "miner_host": m.get("host"),
            "family": m.get("family"),
            "live_online": live_online,
            "live_error": live_error,
        }
        if sample and sample.pools:
            for p in sample.pools:
                rows.append(
                    {
                        **miner_meta,
                        "url": p.url,
                        "user": p.user,
                        "status": p.status,
                        "priority": p.priority,
                        "accepted": p.accepted,
                        "rejected": p.rejected,
                        "stale": p.stale,
                        "last_share_ts": p.last_share_ts,
                        "active": p.active,
                        "slot": p.slot,
                        "ping_ms": p.ping_ms,
                        "ping_loss": p.ping_loss,
                    }
                )
        else:
            # No live pools yet — emit a placeholder so the row still
            # appears. Fields are None because we don't have a sample.
            rows.append(
                {
                    **miner_meta,
                    "url": None,
                    "user": None,
                    "status": None,
                    "priority": None,
                    "accepted": None,
                    "rejected": None,
                    "stale": None,
                    "last_share_ts": None,
                    "active": None,
                    "slot": None,
                    "ping_ms": None,
                    "ping_loss": None,
                }
            )
    return {"pools": rows}


@app.post("/api/miners")
async def api_create_miner(payload: MinerCreate) -> dict:
    """Register a miner by address, auto-detecting the rest.

    Connects to the given IP/hostname and fingerprints it (ports 80 /
    4028) to derive family, port, MAC, model and a friendly name --
    instead of trusting a user-declared family. Best-effort with a hard
    stop: if the host doesn't answer on either port we return 400 so the
    UI can tell the user the miner is unreachable, rather than silently
    persisting a record we could never poll.

    The detected MAC lets :func:`db.upsert_miner` dedupe against an
    existing entry (including the same device on a new IP); user notes
    are layered on top of the detected fields.
    """
    host = (payload.host or "").strip()
    if not host:
        raise HTTPException(400, "host (IP or hostname) is required")

    info = await identify_host(host)
    if info is None:
        raise HTTPException(
            400,
            f"No miner answered at {host} on ports 80 or 4028. "
            "Check the IP/hostname and make sure the device is powered on "
            "and reachable from MinerWatch.",
        )

    if payload.notes is not None:
        info["notes"] = payload.notes

    miner_id = await db.upsert_miner(info)
    return {"id": miner_id}


@app.get("/api/miners/{miner_id}")
async def api_get_miner(miner_id: int) -> dict:
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    latest = await db.latest_metric(miner_id)
    sample = poller.last_results.get(miner_id)
    return {
        "miner": miner,
        "last_metric": latest,
        "live_sample": asdict(sample) if sample else None,
        "capabilities": _capabilities(miner["family"]),
    }


@app.delete("/api/miners/{miner_id}")
async def api_delete_miner(miner_id: int) -> dict:
    await db.delete_miner(miner_id)
    return {"deleted": miner_id}


@app.get("/api/miners/{miner_id}/metrics")
async def api_miner_metrics(
    miner_id: int,
    from_ts: int = 0,
    to_ts: int = 0,
) -> dict:
    import time as _time

    if to_ts == 0:
        to_ts = int(_time.time())
    if from_ts == 0:
        from_ts = to_ts - 24 * 3600
    rows, tier = await db.metrics_range(miner_id, from_ts, to_ts)
    return {
        "miner_id": miner_id,
        "from_ts": from_ts,
        "to_ts": to_ts,
        "tier": tier,
        "metrics": rows,
    }


@app.get("/api/fleet/hashrate_history")
async def api_fleet_hashrate_history(
    minutes: int = 60,
    bucket_seconds: int = 60,
) -> dict:
    """Total fleet hashrate history aggregated by bucket.

    Default: last hour with 1-minute buckets → data points suitable for
    the "1-min average" chart on the home page. ``minutes`` is capped at
    30 days (the same horizon as the 1m-rollup retention) and
    ``bucket_seconds`` is capped at 1 day so callers can ask for sparse
    long-range charts without hitting query-size cliffs. The underlying
    helper auto-routes to the right tier (raw / 1m / 1h) based on the
    requested range.
    """
    import time as _time

    minutes = max(1, min(int(minutes), 30 * 24 * 60))
    bucket_seconds = max(10, min(int(bucket_seconds), 24 * 3600))
    to_ts = int(_time.time())
    from_ts = to_ts - minutes * 60
    points, tier = await db.fleet_hashrate_buckets(from_ts, to_ts, bucket_seconds)
    return {
        "from_ts": from_ts,
        "to_ts": to_ts,
        "bucket_seconds": bucket_seconds,
        "tier": tier,
        "points": points,
    }


@app.get("/api/fleet/block_finds")
async def api_fleet_block_finds(limit: int = 50) -> dict:
    """Return the list of block-found events for the whole fleet.

    Used by the home page to render the celebratory "Blocks found"
    card. Returns the most recent ``limit`` events newest-first; the
    UI typically shows them all (they're so rare that the list is
    short in any reasonable timeframe).
    """
    rows = await db.list_block_finds(limit=max(1, min(int(limit), 500)))
    return {"block_finds": rows}


@app.get("/api/fleet/best_difficulty")
async def api_fleet_best_difficulty() -> dict:
    """Return the fleet's top best-share record per scope.

    Output:
        {
          "session": {"miner_id", "miner_name", "value", "ts"} | None,
          "alltime": {...} | None
        }

    "session" is the best share since the last detected miner reboot
    on whichever device is currently leading. "alltime" is the best
    ever observed by MinerWatch — survives miner reboots, and even
    MinerWatch restarts, because it's persisted in our DB.
    """
    return await db.get_fleet_best_records()


@app.get("/api/fleet/best_difficulty/top")
async def api_fleet_best_difficulty_top(
    scope: str = "alltime",
    limit: int = 10,
) -> dict:
    """Leaderboard dei migliori best-share del fleet per scope.

    Una riga per miner (schema `best_records` ha PK su miner_id+scope).
    ``scope`` può essere 'alltime' o 'session'. ``limit`` clampato a 1..100.
    Pensato per il widget "Top best shares" nella dashboard.
    """
    rows = await db.get_fleet_best_records_ranked(scope=scope, limit=limit)
    return {"scope": scope, "limit": limit, "entries": rows}


# ---------- Live per-share streaming (AxeOS only) ----------
#
# The REST poller only sees aggregates. For AxeOS miners we also tap the
# firmware log WebSocket (backend/log_streamer.py) and surface every
# individual share in real time: a "recent buffer" snapshot for the
# initial paint, an SSE stream for live updates, and a persisted
# near-block Hall of Fame.

def _sse(event: str, data: Any) -> str:
    """Format one Server-Sent Event frame."""
    import json as _json
    return f"event: {event}\ndata: {_json.dumps(data)}\n\n"


@app.get("/api/miners/{miner_id}/shares/recent")
async def api_miner_shares_recent(miner_id: int, limit: int = 1000) -> dict:
    """Snapshot of the in-memory ring buffer of recent share events.

    ``supported`` is False for non-AxeOS miners (Canaan/Braiins/LuxOS),
    which have no per-share log stream; the frontend uses it to show a
    "not available for this miner" state instead of an empty chart.
    """
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    supported = log_streamer.is_supported(miner.get("family"))
    limit = max(1, min(int(limit), 2000))
    return {
        "miner_id": miner_id,
        "supported": supported,
        "events": log_streamer.recent(miner_id, limit) if supported else [],
        "stats": log_streamer.stats(miner_id) if supported else None,
    }


@app.get("/api/miners/{miner_id}/shares/stream")
async def api_miner_shares_stream(miner_id: int) -> StreamingResponse:
    """Server-Sent Events stream of live share events for one AxeOS miner.

    Events:
      - ``snapshot``: {events:[…], stats:{…}} sent once on connect.
      - ``share``:    {seq, ts, diff, target, submitted} per ASIC result.
      - ``verdict``:  {seq, accepted} when the pool grades a submitted
                      share (rare reject → recolour the point red).
    A ``: keepalive`` comment is emitted every 15 s of silence so proxies
    don't time the connection out.
    """
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    if not log_streamer.is_supported(miner.get("family")):
        raise HTTPException(400, "live share streaming is only available for AxeOS miners")

    async def event_gen():
        q = log_streamer.subscribe(miner_id)
        try:
            yield _sse(
                "snapshot",
                {
                    "events": log_streamer.recent(miner_id),
                    "stats": log_streamer.stats(miner_id),
                },
            )
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event["type"], event["data"])
        finally:
            log_streamer.unsubscribe(miner_id, q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        },
    )


@app.get("/api/miners/{miner_id}/notable_shares")
async def api_miner_notable_shares(miner_id: int, limit: int = 50) -> dict:
    """Per-miner near-block Hall of Fame: highest shares, persisted."""
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    limit = max(1, min(int(limit), 500))
    entries = await db.list_notable_shares(miner_id, limit)
    return {
        "miner_id": miner_id,
        "supported": log_streamer.is_supported(miner.get("family")),
        "entries": entries,
    }


@app.get("/api/fleet/prediction")
async def api_fleet_prediction(coin: str = "auto") -> dict:
    """Statistical prediction widget per il fleet.

    ``coin`` controlla quale difficoltà di rete usare per la stima
    "Find a block (solo)":
      * ``auto`` (default) — usa la ``network_difficulty`` riportata via
        stratum dal miner, cioè la moneta che stiamo effettivamente minando
        (comportamento storico, invariato).
      * ``btc`` / ``bch`` — recupera la difficoltà di rete corrente di quella
        moneta da un explorer pubblico (vedi ``coin_difficulty``), così
        l'utente può confrontare le proprie chance cambiando moneta a parità
        di hashrate. Se il recupero fallisce, ``network_difficulty`` resta
        ``None`` e la stima viene semplicemente omessa.


    Calcola la probabilità di battere il best-share all-time corrente
    entro 1h / 24h / 7d, e l'expected time to beat. Se almeno un miner
    online espone ``network_difficulty`` via stratum, calcoliamo anche la
    probabilità di trovare un blocco a difficoltà di rete corrente —
    questa è la metrica che il solo miner vuole davvero vedere.

    Formula (Poisson, share solo-mining):
        rate = H / (D · 2^32)  shares-di-difficolta'-D-per-secondo
        P(t) = 1 - exp(-rate · t)
        E[T] = 1 / rate

    Output:
      {
        "fleet_hashrate_ths": float | None,   # somma device online
        "best_alltime": {value, ts, miner_id, miner_name} | None,
        "network_difficulty": float | None,
        "predictions": {
          "beat_best": {
            "expected_time_s": float | None,
            "probability": {"1h": .., "24h": .., "7d": ..},
          } | None,
          "find_block": {
            "expected_time_s": float | None,
            "probability": {"1h": .., "24h": .., "7d": ..},
          } | None
        }
      }
    """
    import math

    # ---- Hashrate corrente del fleet: somma dei live sample online ----
    fleet_h_ths: float = 0.0
    any_hashrate = False
    network_diff: float | None = None
    miners = await db.list_miners()
    for m in miners:
        sample = poller.last_results.get(m["id"])
        if not sample or not sample.online:
            continue
        if sample.hashrate_ths is not None:
            fleet_h_ths += float(sample.hashrate_ths)
            any_hashrate = True
        # Prendiamo la prima network_difficulty disponibile. Tutti i miner
        # collegati allo stesso pool dovrebbero esporre lo stesso valore;
        # se differiscono, l'ordine di iterazione decide ma la differenza
        # è trascurabile per gli scopi della predizione.
        if network_diff is None and sample.network_difficulty:
            try:
                nd = float(sample.network_difficulty)
                if nd > 0:
                    network_diff = nd
            except (TypeError, ValueError):
                pass

    # ---- Coin override per "Find a block" ----------------------------
    # Default ("auto"): teniamo la difficoltà stratum calcolata sopra.
    # Per btc/bch sostituiamo con la difficoltà di rete di quella moneta
    # presa da un explorer pubblico. Su fallimento azzeriamo network_diff
    # (meglio nessuna stima che una stima su difficoltà sbagliata).
    coin_req = (coin or "auto").strip().lower()
    coin_used = "auto"
    if coin_req in coin_difficulty.supported_coins():
        coin_used = coin_req
        ext_diff = await coin_difficulty.get_difficulty(coin_req)
        network_diff = ext_diff if (ext_diff and ext_diff > 0) else None

    # ---- Best all-time del fleet ----
    best = (await db.get_fleet_best_records()).get("alltime")

    def _prediction(target_diff: float | None) -> dict | None:
        """Per un target di difficoltà, calcola E[T] e P(1h/24h/7d).

        Usa la conversione TH/s → hashes/s (×1e12) e ``D · 2^32`` come
        numero medio di hash per beccare uno share di quella difficoltà.
        """
        if not target_diff or target_diff <= 0:
            return None
        if not any_hashrate or fleet_h_ths <= 0:
            return None
        hashes_per_s = fleet_h_ths * 1e12
        expected_hashes = target_diff * (2.0 ** 32)
        rate = hashes_per_s / expected_hashes  # share/s di quella difficolta'
        if rate <= 0:
            return None
        expected_t = 1.0 / rate
        # Cap exp argument per evitare overflow (in pratica per t enormi
        # P → 1, ma exp(-1e10) sotto-flow è comunque safe in Python).
        def _p(t: float) -> float:
            return 1.0 - math.exp(-min(rate * t, 700.0))
        return {
            "expected_time_s": expected_t,
            "probability": {
                "1h": _p(3600),
                "24h": _p(86400),
                "7d": _p(7 * 86400),
            },
        }

    beat_best = _prediction(best["value"] if best else None)
    find_block = _prediction(network_diff)

    return {
        "fleet_hashrate_ths": round(fleet_h_ths, 4) if any_hashrate else None,
        "best_alltime": best,
        "network_difficulty": network_diff,
        "coin": coin_used,
        "predictions": {
            "beat_best": beat_best,
            "find_block": find_block,
        },
    }


@app.get("/api/miners/{miner_id}/best_difficulty")
async def api_miner_best_difficulty(miner_id: int) -> dict:
    """Per-miner session/all-time best-share records.

    Same shape as the fleet endpoint but scoped to one miner. Missing
    scopes return None (e.g. a brand-new miner with no shares yet).
    """
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    records = await db.get_miner_best_records(miner_id)
    return {
        "miner_id": miner_id,
        "miner_name": miner["name"],
        "session": records["session"],
        "alltime": records["alltime"],
    }


@app.get("/api/miners/{miner_id}/raw")
async def api_miner_raw(miner_id: int) -> dict:
    """Return the raw payload from the most recent poll.

    Handy for debugging when a field isn't being parsed correctly.
    """
    import json as _json

    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    sample = poller.last_results.get(miner_id)
    last_metric = await db.latest_metric(miner_id)
    raw_from_db = None
    if last_metric and last_metric.get("raw"):
        try:
            raw_from_db = _json.loads(last_metric["raw"])
        except (ValueError, TypeError):
            raw_from_db = last_metric["raw"]
    return {
        "miner": {"id": miner["id"], "name": miner["name"], "family": miner["family"], "host": miner["host"]},
        "live_sample": asdict(sample) if sample else None,
        "raw_from_db": raw_from_db,
    }


def _capabilities(family: str) -> dict:
    cls = DRIVERS.get(family)
    if not cls:
        return {}
    return {
        "set_fan": cls.can_set_fan,
        "set_frequency": cls.can_set_frequency,
        "set_voltage": cls.can_set_voltage,
        "restart": cls.can_restart,
        "set_pool": cls.can_set_pool,
    }


# ---------- API: miner controls ----------

class FanPayload(BaseModel):
    percent: int = Field(..., ge=0, le=100)


class FreqPayload(BaseModel):
    mhz: int = Field(..., ge=100, le=2000)


class VoltagePayload(BaseModel):
    millivolts: int = Field(..., ge=800, le=2000)


async def _resolve_driver(miner_id: int):
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    cfg = get_config()
    return miner, driver_for_record(
        {**miner, "timeout": cfg.polling.request_timeout}
    )


@app.post("/api/miners/{miner_id}/control/fan")
async def api_set_fan(miner_id: int, payload: FanPayload) -> dict:
    miner, drv = await _resolve_driver(miner_id)
    if not drv.can_set_fan:
        raise HTTPException(400, f"family {miner['family']} does not support fan control")
    ok = await drv.set_fan_speed(payload.percent)
    if not ok:
        raise HTTPException(502, "the miner rejected the command")
    return {"ok": True}


@app.post("/api/miners/{miner_id}/control/frequency")
async def api_set_frequency(miner_id: int, payload: FreqPayload) -> dict:
    miner, drv = await _resolve_driver(miner_id)
    if not drv.can_set_frequency:
        raise HTTPException(400, f"family {miner['family']} does not support frequency control")
    ok = await drv.set_frequency(payload.mhz)
    if not ok:
        raise HTTPException(502, "the miner rejected the command")
    return {"ok": True}


@app.post("/api/miners/{miner_id}/control/voltage")
async def api_set_voltage(miner_id: int, payload: VoltagePayload) -> dict:
    miner, drv = await _resolve_driver(miner_id)
    if not drv.can_set_voltage:
        raise HTTPException(400, f"family {miner['family']} does not support voltage control")
    ok = await drv.set_voltage(payload.millivolts)
    if not ok:
        raise HTTPException(502, "the miner rejected the command")
    return {"ok": True}


@app.post("/api/miners/{miner_id}/control/restart")
async def api_restart(miner_id: int) -> dict:
    miner, drv = await _resolve_driver(miner_id)
    if not drv.can_restart:
        raise HTTPException(400, f"family {miner['family']} does not support restart via API")
    ok = await drv.restart()
    if not ok:
        raise HTTPException(502, "the miner rejected the command")
    return {"ok": True}


class FanConfigPayload(BaseModel):
    """Per-miner fan control configuration.

    fan_mode:
      - "manual"     → user sets a fixed percentage (`POST /control/fan`)
      - "firmware"   → delegate to the miner's firmware (Avalon `-1`, Bitaxe `autofanspeed=1`)
      - "minerwatch" → server-side PID that nudges the speed to keep
                       chip temp near `auto_target_c`
    """
    fan_mode: Optional[str] = None  # 'manual' | 'firmware' | 'minerwatch'
    auto_target_c: Optional[float] = None
    fan_min_override: Optional[int] = None
    fan_max_override: Optional[int] = None
    fan_threshold_c: Optional[float] = None


@app.post("/api/miners/{miner_id}/control/fan_config")
async def api_set_fan_config(miner_id: int, payload: FanConfigPayload) -> dict:
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    try:
        await db.set_fan_config(
            miner_id,
            fan_mode=payload.fan_mode,
            auto_target_c=payload.auto_target_c,
            fan_min_override=payload.fan_min_override,
            fan_max_override=payload.fan_max_override,
            fan_threshold_c=payload.fan_threshold_c,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # If we just switched to "firmware", send the command to the miner
    # right away to keep state in sync. Bitaxe has set_auto_fan, Avalon
    # uses fan-spd,-1.
    if payload.fan_mode == "firmware":
        cfg = get_config()
        drv = driver_for_record({**miner, "timeout": cfg.polling.request_timeout})
        if hasattr(drv, "set_auto_fan"):
            try:
                await drv.set_auto_fan(True)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True}


# ---------- API: Donate hashrate ----------
# Lets the user lend miners to the project's solo.ckpool address for a
# set time, with automatic revert. See backend/donations.py and
# docs/donate-hashrate-design.md.

class DonationCreate(BaseModel):
    miner_ids: list[int] = Field(..., min_length=1)
    hours: float = Field(..., gt=0, le=72)


@app.get("/api/donations/info")
async def api_donations_info() -> dict:
    """Static info for the Donations page: the address/worker we donate to
    and the duration bounds. Single source of truth for the bounds so the
    UI and server can't drift."""
    from . import donations as dons
    return {
        "btc_address": dons.DONATION_BTC_ADDRESS,
        "worker": dons.DONATION_WORKER,
        "worker_name": dons.donation_worker_name(),
        "pool_url": dons.CKPOOL_SOLO_URL,
        "pool_port": dons.CKPOOL_SOLO_PORT,
        "min_hours": dons.MIN_DONATION_HOURS,
        "max_hours": dons.MAX_DONATION_HOURS,
        "default_hours": dons.DEFAULT_DONATION_HOURS,
    }


@app.get("/api/donations")
async def api_list_donations() -> dict:
    """Flattened active-donations view for the table. Live hashrate and
    pool confirmation come from the poller's last results."""
    from .donations import CKPOOL_SOLO_URL
    rows = await db.list_donation_miners(active_only=True)
    samples = poller.last_results
    out = []
    for r in rows:
        mid = int(r["miner_id"])
        s = samples.get(mid)
        online = bool(s.online) if s else False
        hashrate = s.hashrate_ths if s else None
        pool_url = s.pool_url if s else None
        confirmed = bool(online and pool_url and CKPOOL_SOLO_URL in pool_url)
        out.append({
            "id": r["id"],
            "donation_id": r["donation_id"],
            "miner_id": mid,
            "miner_name": r.get("miner_name"),
            "family": r.get("miner_family"),
            "host": r.get("miner_host"),
            "status": r["status"],
            "ends_ts": r["ends_ts"],
            "seconds_remaining": max(0, int(r["ends_ts"]) - db.now_ts()),
            "online": online,
            "hashrate_ths": hashrate,
            "pool_url": pool_url,
            "confirmed": confirmed,
            "last_error": r.get("last_error"),
        })
    return {"donations": out, "count": len(out)}


@app.post("/api/donations")
async def api_create_donation(payload: DonationCreate) -> dict:
    """Start a donation. Returns 200 with a per-miner breakdown even when
    some miners are rejected (unsupported family / already donating), so
    the UI can explain what happened."""
    return await donation_controller.start_donation(payload.miner_ids, payload.hours)


@app.post("/api/donations/{donation_id}/stop")
async def api_stop_donation(donation_id: int) -> dict:
    """STOP all — revert every in-flight miner in the donation now."""
    don = await db.get_donation(donation_id)
    if not don:
        raise HTTPException(404, "donation not found")
    n = await donation_controller.revert_donation(donation_id)
    return {"ok": True, "reverted": n}


@app.post("/api/donations/{donation_id}/miners/{dm_id}/stop")
async def api_stop_donation_miner(donation_id: int, dm_id: int) -> dict:
    """STOP one miner — the per-row button in the active-donations table."""
    dm = await db.get_donation_miner(dm_id)
    if not dm or int(dm["donation_id"]) != donation_id:
        raise HTTPException(404, "donation miner not found")
    ok = await donation_controller.revert_miner(dm_id)
    return {"ok": ok}


@app.delete("/api/push/subscriptions/all")
async def api_purge_push_subscriptions() -> dict:
    """Remove ALL push subscriptions from the DB.

    Useful when you want to "turn everything off" server-side without
    visiting every single browser/tab that previously subscribed. The
    client-side SW will stop receiving pushes anyway (Chrome gets a 410
    from the push service and self-cleans).
    """
    n = await db.purge_push_subs()
    return {"ok": True, "removed": n}


# ---------- API: Guardian (runtime frequency governor) ----------
#
# A slow, always-on control loop that nudges ASIC frequency to keep the VR
# temperature and HW error rate inside safe bounds, never above a per-miner
# "max frequency" ceiling. Per-miner opt-in; the whole feature is gated
# behind ``cfg.guardian.enabled``. v1 is frequency-only (a v2 voltage lever
# is documented in docs/guardian-design.md). Lives next to the auto-fan PID.

class GuardianConfigPayload(BaseModel):
    # Per-miner opt-in. When enabling without a max, the backend defaults the
    # ceiling to the miner's current frequency (editable afterward).
    enabled: Optional[bool] = None
    # The "max frequency" ceiling the governor never exceeds. Editable by the
    # expert user; defaults to the current frequency on first enable.
    max_freq_mhz: Optional[int] = Field(default=None, ge=100, le=2000)
    # Optional floor override; when omitted the global default is used.
    freq_floor_mhz: Optional[int] = Field(default=None, ge=100, le=2000)


def _miner_current_freq(miner_id: int) -> int | None:
    """Best-effort current frequency: live poll sample first, else None."""
    sample = poller.last_results.get(miner_id)
    if sample and sample.frequency_mhz:
        try:
            return int(sample.frequency_mhz)
        except (TypeError, ValueError):
            return None
    return None


@app.get("/api/miners/{miner_id}/guardian/status")
async def api_guardian_status(miner_id: int) -> dict:
    """Guardian state for a miner: capability, settings, live readout."""
    cfg = get_config()
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    family = (miner.get("family") or "").lower()
    caps = _capabilities(family)
    supported = family in GUARDIAN_FAMILIES and bool(caps.get("set_frequency"))

    current = _miner_current_freq(miner_id)
    if current is None:
        latest = await db.latest_metric(miner_id)
        if latest and latest.get("frequency_mhz"):
            try:
                current = int(latest["frequency_mhz"])
            except (TypeError, ValueError):
                current = None

    g = cfg.guardian
    return {
        "enabled": g.enabled,  # global feature flag
        "supported": supported,
        "miner_enabled": bool(miner.get("guardian_enabled")),
        "max_freq_mhz": miner.get("guardian_max_freq_mhz"),
        "freq_floor_mhz": miner.get("guardian_freq_floor_mhz"),
        "current_freq_mhz": current,
        "defaults": {
            "interval_seconds": g.interval_seconds,
            "vr_high_c": g.vr_high_c,
            "vr_low_c": g.vr_low_c,
            "reject_pct_max": g.reject_pct_max,
            "step_down_vr_mhz": g.step_down_vr_mhz,
            "step_down_err_mhz": g.step_down_err_mhz,
            "step_up_mhz": g.step_up_mhz,
            "frequency_floor_mhz": g.frequency_floor_mhz,
        },
        "live": guardian.status(miner_id),
    }


@app.post("/api/miners/{miner_id}/guardian/config")
async def api_guardian_config(miner_id: int, payload: GuardianConfigPayload) -> dict:
    cfg = get_config()
    if not cfg.guardian.enabled:
        raise HTTPException(404, "the Guardian feature is disabled")
    miner = await db.get_miner(miner_id)
    if not miner:
        raise HTTPException(404, "miner not found")
    family = (miner.get("family") or "").lower()
    caps = _capabilities(family)
    if family not in GUARDIAN_FAMILIES or not caps.get("set_frequency"):
        raise HTTPException(
            400, "the Guardian is only supported on Bitaxe/Nerd* miners"
        )

    # On first enable, default the ceiling to the current frequency so the
    # governor can only hold/back off until the user raises the cap.
    max_freq = payload.max_freq_mhz
    if (
        payload.enabled
        and max_freq is None
        and not miner.get("guardian_max_freq_mhz")
    ):
        max_freq = _miner_current_freq(miner_id)
        if max_freq is None:
            raise HTTPException(
                409,
                "current frequency unknown yet — wait for the first poll, "
                "then enable (or set a max frequency explicitly)",
            )

    await db.set_guardian_config(
        miner_id,
        enabled=payload.enabled,
        max_freq_mhz=max_freq,
        freq_floor_mhz=payload.freq_floor_mhz,
    )
    return {"ok": True, "max_freq_mhz": max_freq}


# ---------- API: discovery ----------

class DiscoveryPayload(BaseModel):
    cidr: Optional[str] = None


@app.post("/api/discovery/scan")
async def api_scan(payload: Optional[DiscoveryPayload] = None) -> dict:
    cidr = payload.cidr if payload else None
    found = await scan_network(cidr=cidr)
    # Import into the DB
    for info in found:
        await db.upsert_miner(info)
    return {"found": found}


@app.post("/api/discovery/auto")
async def api_discovery_auto() -> dict:
    found = await discover_and_register()
    return {"registered": len(found), "miners": found}


# ---------- API: system (host metrics, Raspberry Pi focus) ----------

class SystemFanPayload(BaseModel):
    """Target PWM duty for the host fan (0..100 %)."""
    percent: int = Field(..., ge=0, le=100)


@app.get("/api/system/info")
async def api_system_info() -> dict:
    """Static host info — model, kernel, capabilities.

    Frontend uses ``is_raspberry`` to decide whether to show the
    "System" entry in the sidebar at all. Cheap call (everything is
    precomputed at import time), so the home page can call it once on
    load without measurable latency.
    """
    return system_info.host_info()


@app.get("/api/system/snapshot")
async def api_system_snapshot() -> dict:
    """All dynamic host stats in a single payload. Polled ~every 5 s."""
    return await system_info.snapshot_async(db_path=db_path())


@app.post("/api/system/fan")
async def api_system_set_fan(payload: SystemFanPayload) -> dict:
    """Drive the host fan to the given percent (0..100).

    Returns 400 if no controllable fan is present on this host (e.g.
    running on macOS, or on a Pi without the gpio-fan / pwm-fan kernel
    overlay configured). The UI hides the slider in that case, so this
    is mostly belt-and-braces for direct API users.
    """
    try:
        return await system_info.set_fan_percent_async(payload.percent)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc


# ---------- API: alerts ----------

@app.get("/api/alerts")
async def api_alerts(only_unack: bool = False, limit: int = 200) -> dict:
    rows = await db.list_alerts(limit=limit, only_unack=only_unack)
    return {"alerts": rows}


@app.post("/api/alerts/{alert_id}/ack")
async def api_alert_ack(alert_id: int) -> dict:
    await db.ack_alert(alert_id)
    return {"ok": True}


# ---------- API: settings ----------

class SettingsPayload(BaseModel):
    """Runtime overrides stored in the DB.

    Keys follow the dotted config path, e.g.:
      ``polling.interval_seconds``, ``alerts.temp_chip_threshold``,
      ``auth.enabled``, ``auth.password``, ``storage.retention_days``.
    """

    overrides: Dict[str, Any]


@app.get("/api/settings")
async def api_get_settings() -> dict:
    cfg = get_config()
    # ``asdict(cfg.alerts)`` would echo back the Telegram bot token in
    # plain text — same risk we already avoid for ``auth.password``.
    # Replace it with a boolean flag so the UI can show "✓ configured"
    # without ever revealing the secret.
    alerts_view = asdict(cfg.alerts)
    alerts_view["telegram_token_set"] = bool(alerts_view.pop("telegram_bot_token", "").strip())
    # MQTT view: same posture as the Telegram token — never echo the broker
    # password back. Replace it with a boolean and expose the live connection
    # state so the UI can show "connected" without revealing the secret.
    mqtt_view = asdict(cfg.mqtt)
    mqtt_view["mqtt_password_set"] = bool(mqtt_view.pop("password", "").strip())
    mqtt_view["connected"] = mqtt_publisher.connected
    # Sanitize the raw stored map too: anything sensitive (password,
    # bot token) gets stripped here. Existing callers don't rely on
    # these specific keys being present.
    stored = {
        k: v
        for k, v in (await db.all_settings()).items()
        if k not in {"auth.password", "alerts.telegram_bot_token", "mqtt.password"}
    }
    return {
        "current": {
            "polling": asdict(cfg.polling),
            "alerts": alerts_view,
            "storage": asdict(cfg.storage),
            "network": asdict(cfg.network),
            "auth_enabled": cfg.auth.enabled,
            "mqtt": mqtt_view,
        },
        "stored": stored,
    }


@app.post("/api/settings")
async def api_post_settings(payload: SettingsPayload) -> dict:
    cfg = get_config()
    for key, value in payload.overrides.items():
        await db.set_setting(key, str(value))
    cfg.apply_overrides(payload.overrides)
    # Live-apply MQTT changes by bouncing the publisher, so the user can see
    # it connect/disconnect right after saving without restarting the app.
    if any(k.startswith("mqtt.") for k in payload.overrides):
        try:
            await mqtt_publisher.stop()
            await mqtt_publisher.start()
        except Exception:  # noqa: BLE001
            log.exception("failed to restart MQTT publisher after settings change")
    return {"ok": True}


@app.post("/api/mqtt/test")
async def api_mqtt_test() -> dict:
    """Try connecting to the configured broker (Save first, then Test).

    Mirrors /api/telegram/test: a short connect + publish that confirms the
    broker host/credentials work, surfacing the broker's own error to the UI.
    """
    ok, detail = await mqtt_test_connection()
    if not ok:
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": detail}


@app.post("/api/settings/reload")
async def api_settings_reload() -> dict:
    cfg = reload_config()
    overrides = await db.all_settings()
    cfg.apply_overrides({k: v for k, v in overrides.items() if not k.startswith("_")})
    return {"ok": True}


# ---------- API: auth ----------

class LoginPayload(BaseModel):
    password: str


@app.get("/api/auth/status")
async def api_auth_status() -> dict:
    cfg = get_config()
    return {"enabled": cfg.auth.enabled}


@app.post("/api/auth/login")
async def api_auth_login(
    payload: LoginPayload,
    request: Request,
    response: Response,
) -> dict:
    cfg = get_config()
    if not cfg.auth.enabled:
        return {"ok": True, "auth_disabled": True}

    expected = (cfg.auth.password or "").strip()
    if not expected:
        # Same fail-closed posture as require_auth(): if auth is on but
        # no password is configured we refuse every login attempt instead
        # of letting an empty password match via compare_digest("", "").
        raise HTTPException(
            status_code=401,
            detail="Authentication is enabled but no password is configured",
        )

    # Per-IP rate-limit: a small in-memory counter that locks out a
    # client after LOGIN_FAIL_THRESHOLD consecutive wrong attempts. Keeps
    # brute force on the LAN to a crawl without making typos painful.
    ip = request.client.host if request.client else "unknown"
    remaining = login_lockout_remaining(ip)
    if remaining > 0:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {int(remaining) + 1}s.",
        )

    provided = payload.password or ""
    if not hmac.compare_digest(provided, expected):
        wait = record_login_failure(ip)
        if wait > 0:
            # Just tripped the threshold — surface a 429 so the UI shows
            # the user a useful "locked for N seconds" message instead of
            # a generic "wrong password".
            raise HTTPException(
                status_code=429,
                detail=f"Too many failed attempts. Locked for {int(wait) + 1}s.",
            )
        raise HTTPException(401, "incorrect password")

    # Success — wipe the failure counter for this IP so the next typo
    # doesn't start halfway to a lockout.
    clear_login_failures(ip)

    # Explicit path="/" and a 30-day max_age. The Starlette default is
    # already path="/", but being explicit makes the intent obvious and
    # avoids surprises if a future version changes the default. max_age
    # promotes the cookie from a "session cookie" (which iOS Safari can
    # drop more eagerly) to a persistent one, so users don't have to log
    # in again every time the browser is restarted.
    response.set_cookie(
        "mw_token",
        payload.password,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=60 * 60 * 24 * 30,
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def api_auth_logout(response: Response) -> dict:
    response.delete_cookie("mw_token", path="/")
    return {"ok": True}


# ---------- API: push (Web Push) ----------

class PushSubscription(BaseModel):
    endpoint: str
    keys: Dict[str, str]


@app.get("/api/push/vapid_public_key")
async def api_push_pub_key() -> dict:
    return {"public_key": public_key_b64()}


@app.post("/api/push/subscribe")
async def api_push_subscribe(sub: PushSubscription, request: Request) -> dict:
    p256dh = sub.keys.get("p256dh", "")
    auth_key = sub.keys.get("auth", "")
    if not (sub.endpoint and p256dh and auth_key):
        raise HTTPException(400, "invalid subscription")
    ua = request.headers.get("user-agent")
    await db.add_push_sub(sub.endpoint, p256dh, auth_key, ua)
    return {"ok": True}


@app.delete("/api/push/subscribe")
async def api_push_unsubscribe(payload: dict) -> dict:
    endpoint = payload.get("endpoint")
    if not endpoint:
        raise HTTPException(400, "missing endpoint")
    await db.remove_push_sub(endpoint)
    return {"ok": True}


@app.post("/api/push/test")
async def api_push_test() -> dict:
    """Send a test notification to all registered clients.

    Handy to verify that the push flow works end-to-end without
    having to wait for a real alert.
    """
    from . import alerts as _alerts
    from . import db as _db

    subs = await _db.list_push_subs()
    if not subs:
        raise HTTPException(
            status_code=400,
            detail="No browser is subscribed to push. Open 'Enable notifications' in Settings.",
        )
    await _alerts.send_push(
        {
            "title": "MinerWatch · test",
            "body": "Notifications are working! 🎉",
            "miner_id": None,
        }
    )
    return {"ok": True, "subscribers": len(subs)}


# ---------- API: Telegram ----------

@app.post("/api/telegram/test")
async def api_telegram_test() -> dict:
    """Send a test message to the configured Telegram chat.

    Mirrors ``/api/push/test``: confirms end-to-end that bot token and
    chat_id are valid without waiting for a real alert. Returns the
    error description from Telegram (if any) so the UI can show it.
    """
    from . import alerts as _alerts

    ok, detail = await _alerts.send_telegram(
        {
            "title": "MinerWatch · test",
            "body": "Telegram notifications are working! 🎉",
        }
    )
    if not ok:
        # 400 keeps the same convention as /api/push/test for "you need
        # to configure things first".
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True}


@app.get("/api/telegram/discover_chat_id")
async def api_telegram_discover_chat_id() -> dict:
    """Help the user find the chat_id for the currently-configured bot.

    Calls Telegram's ``getUpdates`` and extracts the distinct chats
    seen recently. The user just sent ``/start`` to the bot from their
    phone — the chat shows up here, they click it in the UI and the
    chat_id field gets populated automatically.

    Note: Telegram drops updates after ~24h, and ``getUpdates`` is
    incompatible with webhooks. We never set a webhook so this is
    safe to call repeatedly.
    """
    from . import alerts as _alerts

    raw = await _alerts.telegram_get_updates()
    if not raw.get("ok"):
        # Surface both our own errors (missing token, network) and
        # Telegram's (invalid token → "Unauthorized") to the UI.
        error = raw.get("error") or raw.get("description") or "unknown error"
        raise HTTPException(status_code=400, detail=error)

    seen: dict[str, dict[str, Any]] = {}
    for update in raw.get("result", []):
        # Telegram messages can come as ``message``, ``edited_message``,
        # ``channel_post``, etc. We unify all of them.
        msg = (
            update.get("message")
            or update.get("edited_message")
            or update.get("channel_post")
            or update.get("my_chat_member")
        )
        if not isinstance(msg, dict):
            continue
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        if cid is None:
            continue
        key = str(cid)
        if key in seen:
            continue
        # Build a human-friendly label: prefer username, then first/last
        # name, then chat title for groups. Fall back to the raw id.
        username = chat.get("username")
        first = chat.get("first_name")
        last = chat.get("last_name")
        title = chat.get("title")
        ctype = chat.get("type") or "?"
        if title:
            label = f"{title} ({ctype})"
        elif first or last:
            full = " ".join(p for p in (first, last) if p)
            label = f"{full}" + (f" @{username}" if username else "")
        elif username:
            label = f"@{username}"
        else:
            label = key
        seen[key] = {"chat_id": key, "label": label, "type": ctype}

    return {"ok": True, "chats": list(seen.values())}





# ---------- SPA catch-all ----------
#
# Anything that hasn't matched an /api/*, /assets/*, /sw.js or
# /favicon route up to here is a client-side route owned by React
# Router. Serve dist/index.html so the bundle takes over and resolves
# the URL on the browser side.
#
# This route is registered last on purpose: FastAPI matches routes in
# registration order, and a /{path:path} catch-all defined earlier
# would shadow every API endpoint above.

@app.get("/", include_in_schema=False)
async def spa_root() -> Response:
    return _react_index_response()


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_catchall(full_path: str) -> Response:  # noqa: ARG001
    return _react_index_response()

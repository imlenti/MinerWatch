<div align="center">

# MinerWatch

**A local-first dashboard for home Bitcoin miners.**

Monitor and control Bitaxe, NerdQAxe / NerdOCTAXE, Canaan Avalon Nano 3s
and Braiins BMM miners — plus read-only monitoring of LuxOS Antminer /
Whatsminer rigs — on your home network, all from your browser. No cloud,
no telemetry.

[![CI](https://github.com/imlenti/MinerWatch/actions/workflows/ci.yml/badge.svg)](https://github.com/imlenti/MinerWatch/actions/workflows/ci.yml)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Status](https://img.shields.io/badge/status-alpha-orange.svg)](#)
[![No Warranty](https://img.shields.io/badge/warranty-none-red.svg)](#disclaimer)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

⚡ If MinerWatch is useful to your home rig, donations are welcome — BTC only:
`bc1qexhamvrpclpr2skyyw3u8edm8kznnvt6zjudxu`

![MinerWatch dashboard](docs/screenshots/dashboard.png)

</div>

## Screenshots

<table>
  <tr>
    <td align="center">
      <strong>Dashboard</strong><br/>
      <sub>Fleet overview · live miner cards · hashrate chart</sub><br/>
      <img src="docs/screenshots/dashboard.png" alt="Dashboard" width="100%"/>
    </td>
    <td align="center">
      <strong>Miner controls</strong><br/>
      <sub>Per-device Controls tab · fan slider · AUTO PID</sub><br/>
      <img src="docs/screenshots/miner-controls.png" alt="Miner controls" width="100%"/>
    </td>
  </tr>
  <tr>
    <td align="center">
      <strong>Analytics</strong><br/>
      <sub>Beat-best & find-block predictions · top best shares</sub><br/>
      <img src="docs/screenshots/analytics.png" alt="Analytics" width="100%"/>
    </td>
    <td align="center">
      <strong>System</strong><br/>
      <sub>Raspberry Pi host metrics · throttling · CPU temp</sub><br/>
      <img src="docs/screenshots/system.png" alt="System" width="100%"/>
    </td>
  </tr>
  <tr>
    <td align="center">
      <strong>Pools</strong><br/>
      <sub>Every configured pool · accepted/rejected · ping latency</sub><br/>
      <img src="docs/screenshots/pools.png" alt="Pools" width="100%"/>
    </td>
    <td align="center">
      <strong>Live shares</strong><br/>
      <sub>Real-time per-share stream · diff vs target (AxeOS)</sub><br/>
      <img src="docs/screenshots/liveshares.png" alt="Live shares" width="100%"/>
    </td>
  </tr>
  <tr>
    <td align="center">
      <strong>Donate hashrate</strong><br/>
      <sub>Lend miners to the project solo pool · auto-revert</sub><br/>
      <img src="docs/screenshots/donations.png" alt="Donate hashrate" width="100%"/>
    </td>
    <td align="center">
      <strong>Update</strong><br/>
      <sub>Version check · release notes · update flow</sub><br/>
      <img src="docs/screenshots/update.png" alt="Update" width="100%"/>
    </td>
  </tr>
</table>



---

## What it is

MinerWatch is a small Python web app you run on your own machine (a Mac, a
Linux box, or a Raspberry Pi) on the same LAN as your miners. It polls each
device every few seconds, stores hash rate, temperature, power, fan and pool
data in a local SQLite database, and gives you a browser dashboard that
works from your phone, tablet, or laptop.

It is meant for **home / small-scale mining setups** (1–10 miners), users
comfortable opening a terminal but not necessarily developers.

## Features

- **Live dashboard** — hash rate, chip and VR temps, fans, power, efficiency
  (W/TH), accepted / rejected shares and uptime for every miner at a glance,
  refreshed every 5 seconds by default
- **Per-miner detail page** with tabs (Overview · Hardware · History ·
  Controls), Chart.js graphs over the last hour / day / week / month, and a
  grouped hardware view (Identity, ASIC, Thermal, Fan & Power, Pool)
- **Predictions widget** — probability of beating your all-time best share
  and (when stratum exposes the network difficulty) of finding a block, at
  1 h / 24 h / 7 d, computed with the Poisson model for solo-mining shares
- **Top best shares leaderboard** across enabled miners, with medals and a
  link to the device
- **Best-share tracker** — session and all-time best difficulty per miner
  and across the fleet, with native push when a miner breaks its own
  all-time record
- **Block-find detection** — when a share difficulty meets or exceeds the
  network difficulty (a solo block!), MinerWatch fires a push notification
  and pins a permanent trophy card on the dashboard
- **Server-side auto-fan PID** controller (`backend/auto_control.py`)
  replicating the BitAxe firmware loop (Kp=5, Ki=0.1, Kd=2, EMA, target temp
  configurable per device). Useful on miners whose firmware lacks a sane
  curve, or to hold a target temperature across the fleet
- **Guardian frequency governor** (`backend/guardian.py`, AxeOS / Nerd\*
  only) — a slow outer-loop control that watches the *VR (voltage
  regulator)* temperature and the rejected-share rate, and nudges the
  ASIC frequency down when the VR runs hot or shares get unstable, then
  recovers frequency as it cools. Complements the fan PID (which watches
  the *chip*) and fills the gap the 75 °C overheat watchdog doesn't cover.
  Opt-in per miner with editable floor / ceiling. Design notes:
  [docs/guardian-design.md](docs/guardian-design.md)
- **Donate hashrate** — temporarily lend any subset of your miners to the
  project's solo.ckpool address for a few hours; MinerWatch snapshots each
  miner's current pool, repoints it, and auto-reverts when the timer
  expires (crash-safe across restarts). One-click from the *Donate*
  sidebar entry. Design notes:
  [docs/donate-hashrate-design.md](docs/donate-hashrate-design.md)
- **Optional MQTT publisher** for Home Assistant — connects to a broker
  you already run and pushes a retained state blob per miner, with HA
  MQTT-discovery so each miner auto-appears as an HA device, plus optional
  flat scalar topics for ESP32 / ESPHome panels. Self-disables if not
  configured. Full guide:
  [docs/home-assistant-integration.md](docs/home-assistant-integration.md)
- **Multi-channel alerts**: Web Push (VAPID) for native OS notifications,
  *and* a Telegram bot that delivers to any phone or desktop without HTTPS
  — both channels are independent kill-switches in the UI
- **Tiered metric retention** — raw 5-second samples for the last 48 h,
  1-minute rollups for 30 days, 1-hour rollups for 2 years. SQLite stays
  small, history stays long
- **LAN auto-discovery** of miners on demand (port 80 for Bitaxe-class,
  port 4028 for cgminer-based devices), with MAC-pinned identity so DHCP
  IP changes don't break tracking
- **Optional password protection** (bearer-token + cookie) for setups where
  the LAN isn't fully trusted
- **Modern dark UI** — React 18 + TypeScript + Tailwind CSS + Shadcn primitives
  + Recharts, served as a single-page app. Sidebar nav, tabbed device detail,
  responsive down to phone-size viewports
- **One-click macOS launcher**, Docker setup for Linux / Raspberry Pi, and
  an Umbrel App Store package (`umbrel/`) for one-click install on umbrelOS
- **In-app self-update** (bare-metal installs) — sidebar entry checks
  GitHub Releases, downloads and SHA-256-verifies the matching tarball,
  swaps files preserving your data, and restarts the service, all from
  the dashboard with no SSH. Docker and Umbrel update via their image
  instead — see [Updates](#updates)
- **Self-healing frontend bundle** — if `frontend-react/dist/` is missing
  or out of sync with the installed version (typical after a `git pull`
  or fresh clone), `start.sh` automatically fetches the prebuilt bundle
  from the matching GitHub release on first launch. Node.js is **not
  required** on the host
- **No cloud, no account, no analytics** — all data stays on your box

## Supported miners

| Family            | Tested models                              | Protocol                          | Controls   |
|-------------------|--------------------------------------------|-----------------------------------|------------|
| Bitaxe            | Gamma 601 / 602, Supra, Ultra, Max         | HTTP REST :80                     | Full       |
| NerdQAxe / Octaxe | NerdQAxe+, NerdQAxe++, NerdOCTAXE-Plus/Gamma | HTTP REST :80 (Bitaxe-compatible) | Full       |
| Canaan Avalon     | Nano 3s                                     | TCP cgminer-text :4028           | Full       |
| Braiins           | BMM 101 (BOSminer firmware)                | TCP cgminer-JSON :4028           | Full       |
| LuxOS (Luxor)     | Bitmain Antminer S19 / S21, MicroBT Whatsminer | TCP cgminer-JSON :4028        | Monitor only |

The **NerdQAxe and NerdOCTAXE** lines are one driver family internally
(`nerdoctaxe`): both are multi-chip AxeOS boards that share the Bitaxe
REST surface, so MinerWatch auto-detects either as the same family and
adds the dual-fan / dual-pool / PSU-current readouts on top.

**LuxOS is monitoring-only for now.** MinerWatch reads hashrate, temps,
fans, power and pools from LuxOS-flashed Antminer/Whatsminer rigs, but
exposes no control buttons — LuxOS write commands need a stateful
session that's intentionally left for a later release. Everything else
(Bitaxe, Nerd*, Canaan, Braiins) supports fan / frequency / voltage /
restart control.

Adding a new model usually means a single new file in `backend/miners/`.
See [CONTRIBUTING.md](CONTRIBUTING.md) for the driver template.

## Quick start

### macOS / Linux (one-line)

```bash
git clone https://github.com/imlenti/MinerWatch.git
cd MinerWatch
chmod +x start.sh
./start.sh
```

That's it. `start.sh`:

1. Creates a Python virtualenv in `.venv/` and installs the backend deps.
2. **Frontend auto-heal** — if `frontend-react/dist/` is missing or out of
   sync with `VERSION`, it downloads the prebuilt bundle from the matching
   GitHub release (~1.5 MB, no Node required). Falls back to a local
   `npm install && npm run build` only if the download fails *and* Node is
   available.
3. Initialises the SQLite database in `data/`.
4. Starts the FastAPI server.

Then open:

- From the same machine: <http://localhost:8000>
- From any phone / tablet / PC on the same LAN: `http://<host-ip>:8000`

Stop with `Ctrl+C`, or `./stop.sh` if it was launched in the background.

> **Want to build the frontend yourself?** (e.g. you're hacking on the
> React side) Install Node ≥ 18 (`brew install node` on macOS, or
> `curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt-get install -y nodejs`
> on Raspbian), then run `cd frontend-react && npm install && npm run build`.
> To skip the autoheal entirely (e.g. you're running `vite dev` in
> parallel), set `MINERWATCH_SKIP_FRONTEND_AUTOHEAL=1` before launching
> `start.sh`.

### Windows (via WSL2)

Windows is supported through **WSL2** (Windows Subsystem for Linux), which
runs a real Linux kernel — so MinerWatch runs exactly as it does on Linux,
using the same `start.sh` / `scripts/install-service.sh` flow. There is no
separate Windows build to maintain, and the in-app **Update** button works
(it relies on systemd inside WSL2, just like a native Linux install).

1. Install WSL2 with Ubuntu (run in PowerShell as Administrator, then reboot):

   ```powershell
   wsl --install
   ```

2. Open the **Ubuntu** terminal and follow the Linux quick start:

   ```bash
   sudo apt update && sudo apt install -y python3-venv git
   git clone https://github.com/imlenti/MinerWatch.git
   cd MinerWatch
   chmod +x start.sh
   ./start.sh
   ```

   Open <http://localhost:8000> in your Windows browser.

3. *(Optional)* Auto-start at login: enable systemd in WSL2 by adding the
   following to `/etc/wsl.conf`, then run `wsl --shutdown` from PowerShell
   and reopen Ubuntu:

   ```ini
   [boot]
   systemd=true
   ```

   Then register the service as on Linux: `./scripts/install-service.sh`.

> **Networking caveat.** By default WSL2 sits behind a virtual NAT, so LAN
> **auto-discovery won't see your `192.168.x.x` miners** and other devices on
> the LAN can't reach the dashboard. Two fixes:
>
> - **Mirrored networking** (Windows 11 22H2+, recommended): create
>   `C:\Users\<you>\.wslconfig` with:
>
>   ```ini
>   [wsl2]
>   networkingMode=mirrored
>   ```
>
>   then `wsl --shutdown` and reopen. WSL2 now shares the Windows host's
>   network, so discovery and LAN access work like a native install.
> - Or just **add miners manually by IP** from the *Add miner* button — the
>   polling layer reaches miners at their LAN IPs through the NAT in most
>   setups even without mirrored mode.

### macOS one-click (recommended for non-developers)

Double-click `installer.command` from Finder. It will:

1. Copy MinerWatch into `~/Library/Application Support/MinerWatch/`.
2. Create the Python virtualenv there and install dependencies.
3. Register a **LaunchAgent** so MinerWatch starts automatically every
   time you log in, and restarts itself if it ever crashes.
4. Open the dashboard at `http://localhost:8000` in your browser.

The installer works no matter where you keep the source folder — Desktop,
Documents, Downloads, iCloud Drive, an external volume, anywhere. macOS
Privacy (TCC) blocks background launchd jobs from reading those locations,
so MinerWatch installs the running copy under `~/Library/Application
Support` (always accessible) and runs from there. After install, you can
move or delete the source folder; the service keeps running.

To update to the latest published release, open the dashboard, click
**Update** in the sidebar and hit *Install*. If a new release exists on
GitHub, MinerWatch downloads + verifies + swaps + restarts itself in
~20 s, preserving your `data/` and `config.yaml`. See the
[Updates](#updates) section below for the full flow.

To update after editing the source locally instead: double-click
`installer.command` again — it re-syncs the runtime copy from the
source folder.

To stop the auto-start, double-click `uninstaller.command`. It will offer
to wipe the runtime directory too (database + logs); answer `n` to keep
your data.

### Run as a service (auto-start at login / boot)

If you skipped the one-click installer, you can register the service
manually. The same script handles both macOS (launchd) and Linux (systemd):

```bash
./scripts/install-service.sh           # install + start
./scripts/install-service.sh --status  # show current state
./scripts/uninstall-service.sh         # remove
```

On **macOS** this installs a user-level LaunchAgent at
`~/Library/LaunchAgents/com.imlenti.minerwatch.plist`. Logs land in
`data/logs/minerwatch.{out,err}.log`.

On **Linux / Raspberry Pi** it installs a systemd user unit at
`~/.config/systemd/user/minerwatch.service`. Tail the logs with
`journalctl --user -u minerwatch -f`. To start MinerWatch at boot even
without an interactive login (typical headless Pi setup), enable lingering:

```bash
sudo loginctl enable-linger $USER
```

### Docker / Raspberry Pi (alternative to start.sh)

A multi-stage `Dockerfile` and a `docker-compose.yml` are shipped for
users who prefer containers. **It's an alternative, not a requirement**:
on Linux (including Raspberry Pi) `start.sh` and `scripts/install-service.sh`
work just as well, with less overhead — and the in-app update flow
works only on the bare-metal install, not under Docker (see below).

```bash
docker compose up -d --build
```

First build takes 1–3 minutes (downloads the Python image and resolves
the dependency tree); subsequent restarts are instant.

What the stack does:

- Builds a Python 3.11-slim image, multi-stage, runs as a non-root
  `minerwatch` user (UID 1000), no compilers in the final layer.
- Mounts `./data` from the repo into `/app/data` so SQLite, VAPID
  keys, push subscriptions and logs survive `docker compose down`.
- Uses `network_mode: host` so MinerWatch can reach miners on the
  same LAN as the host — required for auto-discovery and polling.
- Adds a `HEALTHCHECK` on `/api/health` so `docker ps` reflects
  whether the API is actually serving.

Update by rebuilding the image after a `git pull` (Docker doesn't support
the in-app Update button — the image is immutable; full reasoning in
[Updates](#updates)):

```bash
git pull origin main
docker compose up -d --build
```

If you want the in-app, one-click update flow, switch to the bare-metal
install with `./scripts/install-service.sh` — your `data/` directory is
already a bind-mount on the host, so the migration is just *stop the
container, install the service pointing at the same folder*.

Stop:

```bash
docker compose down
```

Wipe runtime data too (database, VAPID keys, push subscriptions —
will reset everything as if newly installed):

```bash
docker compose down
rm -rf data
```

### Umbrel App Store

If you run an Umbrel home server, MinerWatch ships an App Store package
under `umbrel/` (`umbrel-app.yml` + `docker-compose.yml` + README). The
submission process is the standard `getumbrel/umbrel-apps` PR flow —
the `umbrel/README.md` documents the build / pin / publish checklist.
There's also a ready-to-publish **Community App Store** under
`community-app-store/`, which Umbrel users can add by URL without waiting
for official-store review (*App Store → ⋯ → Community App Stores →* paste
the repo URL). Either way, installation is a single click from the
Umbrel dashboard, and persistent data lives in `${APP_DATA_DIR}/data`
on the Umbrel box.

**Updating on Umbrel**: the in-app *Update* button is disabled under
Umbrel (the container image is immutable), but you don't need it — when
a newer version is published, Umbrel shows an **Update** badge on the app
in the App Store, and one click pulls the new image. Your
`${APP_DATA_DIR}/data` (database, push keys, settings) is preserved
across the update.

#### Caveat: Docker Desktop on macOS / Windows

Docker Desktop runs containers inside a Linux VM, which means
`network_mode: host` is silently dropped to bridge mode. The dashboard
will still be reachable at <http://localhost:8000>, but **auto-discovery
will not find any miner** because the container can't see your
`192.168.x.x` network. Workarounds:

- Add miners manually by IP from the *Add miner* button in the UI
  (the polling layer routes through Docker Desktop's NAT and reaches
  miners at their LAN IPs from inside the VM in most setups).
- Or — strongly recommended on macOS — use `installer.command`
  instead. The native LaunchAgent has full LAN access without any
  of the VM hops.

## Architecture

```
                  ┌─────────────────────────┐    ┌─────────────────┐
                  │  Browser (React SPA,    │    │   Telegram app  │
                  │  Vite + Tailwind +      │    │   (phone, etc)  │
                  │  Shadcn + Recharts)     │    └────────▲────────┘
                  └────────────┬────────────┘             │  Bot API
                               │  HTTP / WebPush          │
                               ▼                          │
   ┌─────────────────────────────────────────────────────────────────┐
   │                          FastAPI app                            │
   │  main.py  ·  auth.py  ·  alerts.py  ·  auto_control.py (PID)    │
   │                                                                 │
   │   ┌────────────┐  ┌──────────────┐  ┌─────────────────────┐     │
   │   │  poller    │  │  discovery   │  │  alerts dispatcher  │     │
   │   │ (asyncio,  │  │ (LAN /24     │  │  WebPush + Telegram │     │
   │   │  every 5s) │  │  on demand)  │  │ in parallel         │     │
   │   └─────┬──────┘  └──────┬───────┘  └─────────────────────┘     │
   │         │                │                                      │
   │         ▼                ▼                                      │
   │   ┌────────────────────────────────────────┐                    │
   │   │       miners/  driver layer            │                    │
   │   │   bitaxe · canaan · braiins (poll +    │                    │
   │   │   fan/freq/voltage/restart write API)  │                    │
   │   └──────────────┬─────────────────────────┘                    │
   │                  │                                              │
   │                  ▼                                              │
   │           ┌─────────────┐                                       │
   │           │  SQLite db  │   (data/minerwatch.db)                │
   │           │  raw 48h /  │                                       │
   │           │  1m 30d /   │                                       │
   │           │  1h 2y      │                                       │
   │           └─────────────┘                                       │
   └─────────────────────────────────────────────────────────────────┘
                              │
                              ▼  TCP / HTTP polling every 5 s
                     ┌─────────────────┐
                     │  Miners on LAN  │
                     └─────────────────┘
```

## Configuration

The shipped defaults work for most home setups. To customise, copy
`config.example.yaml` to `config.yaml` and edit, **or** use the in-app
**Settings** page (UI changes take precedence over the file and are stored in
the database).

Highlights:

- `polling.interval_seconds` — how often miners are polled (default 5 s)
- `polling.hashrate_smoothing_seconds` — tau of the server-side EMA
  applied to hashrate (default 60 s, set 0 to see raw firmware values)
- `network.scan_cidr` — subnet for auto-discovery (`auto` picks the host's
  current LAN, e.g. `192.168.1.0/24`)
- `alerts.temp_chip_threshold`, `alerts.temp_vr_threshold`,
  `alerts.offline_threshold_seconds`, `alerts.repeat_seconds` —
  thresholds and re-alert cadence
- `alerts.push_enabled`, `alerts.telegram_enabled` — independent
  kill-switches for the two notification channels
- `storage.retention_raw_hours` / `retention_1m_days` /
  `retention_1h_days` — tiered retention (defaults: 48 h / 30 d / 730 d).
  The legacy `storage.retention_days` is honoured as an alias for the
  1-minute tier so older configs keep working
- `auth.enabled`, `auth.password` — turn on password protection
- `mqtt.*` — optional Home Assistant / MQTT publisher (off by default;
  see [docs/home-assistant-integration.md](docs/home-assistant-integration.md))

Every key is documented inline in
[config.example.yaml](config.example.yaml), and all of them are also
editable from the in-app **Settings** page.

## Updates

MinerWatch versions itself with a plain `VERSION` file in the repo root.
Three places use it:

- `GET /api/version` — the installed version.
- `GET /api/update/check` — compares against the latest GitHub Release
  (`github.com/imlenti/MinerWatch/releases/latest`) and tells the
  frontend whether an update is available.
- `POST /api/update/install` — downloads the release tarball, verifies
  its SHA-256 against the `checksums.txt` published with the release,
  swaps the new files into place (skipping `data/`, `.venv/`,
  `config.yaml`, `data/vapid_keys.json`), and `os._exit(1)`s. The
  service manager (launchd on macOS, systemd on Linux) relaunches the
  process and `start.sh` picks up any new Python dependencies.

In the dashboard, open the **Update** sidebar entry: it shows the
installed version, the latest release on GitHub, a link to the
release notes, and an *Install* button. A red dot on the sidebar
entry signals that an update is available. The check is cached on
disk for 6 hours (avoids hitting the anonymous GitHub rate limit of
60 req/h) and can be forced with the *Check now* button.

**What's preserved on an update**: the SQLite DB, VAPID keys, push
subscriptions, your `config.yaml`, and the Python virtualenv. The
update only ever overwrites code files.

**How updates work per install type:**

- **Bare-metal** (macOS launchd / Linux systemd, via `installer.command`
  or `scripts/install-service.sh`): the in-app *Install* button does the
  full download → verify → swap → restart. This is the only mode where
  the one-click button applies.
- **Docker**: the in-app button is disabled (the image is immutable, so a
  file swap would be discarded on the next container recreate). Update by
  pulling the new image — see the
  [Docker section](#docker--raspberry-pi-alternative-to-startsh).
- **Umbrel**: also image-based, so the in-app button is disabled — but
  Umbrel surfaces an **Update** badge on the app when a new version ships,
  and one click from the App Store pulls it. No SSH or rebuild needed.

**Cutting a release** (if you fork or maintain MinerWatch): bump
`VERSION` and `frontend-react/package.json`, tag with `vX.Y.Z` and
push. The `.github/workflows/release.yml` workflow builds the
frontend, packs `minerwatch-X.Y.Z.tar.gz` (including the prebuilt
`dist/`), writes `checksums.txt`, and publishes a GitHub Release.
Every installation on `vX.Y.(Z-1)` or older will see the new release
within 30 minutes (or immediately via *Check now*).

## Notifications

MinerWatch ships with two independent alert channels you can enable
side-by-side. The dispatcher fans the same alert payload out to both in
parallel, so a failure on one channel never blocks the other.

Alerts fire for:

- Chip / VR temperature over threshold
- Miner offline beyond `offline_threshold_seconds`
- Miner coming back online (recovery)
- New all-time best share record
- Block found (share difficulty ≥ network difficulty)

Re-alerts fire every `alerts.repeat_seconds` (default 600 s) while a
critical condition persists, with a sticky banner in the dashboard.

### Browser push (Web Push + VAPID)

On first launch MinerWatch generates a VAPID key pair and stores it in
`data/vapid_keys.json` (treat this as private — it identifies your server
to subscribed browsers). On *Settings → Notifications* click *Enable
notifications* to grant your browser permission. From then on you'll get
native OS notifications.

> **macOS note**: in addition to the per-site permission, you also need to
> allow notifications at the system level under *System Settings →
> Notifications → Google Chrome*. See the
> [Troubleshooting](#troubleshooting) section below.

> **HTTPS caveat**: browsers expose the Web Push API only on `https://`
> or `http://localhost`. If you reach MinerWatch from another device on
> the LAN (`http://192.168.1.x:8000`) push will show as "not supported"
> on that browser — use the Telegram channel below instead.

### Telegram bot

Works on any device (iPhone, Android, desktop) and doesn't require HTTPS,
because MinerWatch (the server) is what calls the Telegram Bot API.

1. Open Telegram, talk to **@BotFather**, send `/newbot`, copy the token.
2. *Settings → Notifications*: paste the token, click *Save all*.
3. Open your new bot in Telegram and send `/start` (or any message) so
   the bot can see your chat.
4. Click **Find my chat ID** in MinerWatch. Pick your chat → the ID
   gets filled in automatically.
5. Tick **Send Telegram notifications**, click *Save all*, and **Send
   test message** to confirm.

For a group: add the bot to the group, send any message, then use
"Find my chat ID" — group IDs are negative (e.g. `-1001234567890`).

## Optional password protection

The defaults assume your home LAN is trusted, so no password is required.
To turn auth on, open *Settings → Security → Password protection*, set a
password, and save — every API request and frontend page will then
require authentication.

Under the hood MinerWatch accepts either an `Authorization: Bearer
<password>` header or an `mw_token=<password>` cookie. The cookie is set
automatically when you submit the login form at `/login`, so the
dashboard "just works" in a browser; the bearer header is there for
scripted access and curl.

Failed logins are rate-limited per client IP: after a handful of wrong
passwords that IP is locked out for 60 seconds, which slows brute-force
attempts without inconveniencing a legitimate typo. If `auth.enabled` is
true but no password is set, MinerWatch fails closed (refuses every
protected request) rather than silently allowing access.

## Discovery

When you click **Scan network** on the dashboard, MinerWatch scans the
configured subnet for ports 80 (Bitaxe-class) and 4028 (cgminer / Avalon
/ Braiins) and auto-registers any miner it finds. Devices are identified
by MAC, so DHCP lease changes don't break the time series.

Discovery is **on demand** by design — a continuous /24 scan would make
noise on the LAN every few minutes and isn't necessary on a stable home
network. Re-run *Scan network* whenever you add or move a miner, or add
one manually by hostname / IP from *Add miner*.

The default `network.scan_cidr: "auto"` resolves at runtime to the
host's own /24 (e.g. if the Mac/Pi has IP `192.168.0.42`, the scanned
range is `192.168.0.0/24` — all 254 host addresses, from `.1` to
`.254`). You only need to override the CIDR manually if:

- the host has more than one active network interface (Wi-Fi + Ethernet,
  Wi-Fi + VPN, Wi-Fi + Thunderbolt bridge…) and the miners live on the
  one that isn't the default route — set the CIDR to the right LAN from
  *Settings → Network*;
- your LAN is wider than /24 (e.g. an enterprise /22 or /16) — set the
  CIDR to the actual range so all miners are covered;
- multiple VLANs are bridged through the same host — pick the one with
  the miners or run discovery once per VLAN with manual CIDRs.

## Troubleshooting

<details>
<summary><b>start.sh fails creating the virtualenv on macOS</b></summary>

Apple's bundled Python sometimes ships a broken `venv` module. Run
`./diagnose.sh` first — it tells you exactly what's wrong. The usual fix is:

```bash
brew install python
PYTHON_BIN=$(brew --prefix)/bin/python3 ./start.sh
```
</details>

<details>
<summary><b>Auto-discovery doesn't find any miner</b></summary>

99% of the time this is a wrong-subnet issue, not a connectivity one.
Open the logs (`data/logs/minerwatch.out.log` or
`journalctl --user -u minerwatch -f`) and look for the line
`Discovery: scanning <CIDR>`.

- If the CIDR shown is *not* the network your miners are on (e.g. logs
  say `192.168.1.0/24` but the miners are on `192.168.0.x`), set
  `network.scan_cidr` from the *Settings* page to the right CIDR.
  Common cases: the host has multiple interfaces (Wi-Fi + Ethernet,
  Wi-Fi + VPN), or the LAN is bigger than /24.
- If the log says
  `could not auto-detect the host's subnet`, the host has no default
  route at all. Set the CIDR manually from Settings.

You can always add a miner by IP/hostname from *Add miner* without
relying on discovery.
</details>

<details>
<summary><b>Push notifications are silent on macOS</b></summary>

Two layers of permission are required:

1. *In Chrome*: site permission for `http://localhost:8000` →
   Notifications → Allow
2. *In macOS*: System Settings → Notifications → Google Chrome → Allow
   notifications, with banner / alert style of your choice
</details>

<details>
<summary><b>Push fails with a "key parsing" or LibreSSL error</b></summary>

This is a known issue with Apple's LibreSSL. MinerWatch already works
around it by feeding the VAPID private key in raw base64 (not PEM). If
you see this error and you're not on macOS, please open an issue with
your `pip show pywebpush` and `python -c "import ssl; print(ssl.OPENSSL_VERSION)"`.
</details>

<details>
<summary><b>Braiins BMM 101 shows zeros for temperatures / fans</b></summary>

Braiins firmware doesn't populate `temps` / `fans` / `tunerstatus` on every
build. MinerWatch falls back gracefully but you'll see partial data. Try
upgrading to the latest BOSminer release.
</details>

<details>
<summary><b>Canaan Nano 3s power readings look off</b></summary>

Power is read from the `MPO[N]` field (watts, direct). The legacy `PS[...]`
fields use different units depending on firmware, so they're ignored.
</details>

## Adding a new miner driver

In short: drop a new file in `backend/miners/`, subclass `MinerDriver`,
implement `async def poll(self) -> MinerSample`, optionally override the
write methods (`set_fan_speed`, `set_frequency`, `set_voltage`, `restart`)
plus the capability flags, and register it in `backend/miners/__init__.py`.
Full walkthrough with a copy-pasteable template in
[CONTRIBUTING.md](CONTRIBUTING.md).

## Roadmap

Done:

- [x] Best-share tracker (session / all-time per miner + fleet) with push
- [x] Block-find detection + trophy card
- [x] Server-side auto-fan PID controller
- [x] Telegram bot in addition to Web Push
- [x] Solo-lottery odds card (network difficulty vs your hashrate)
- [x] Top best shares leaderboard
- [x] Per-miner Hardware tab with grouped readouts
- [x] Umbrel App Store package
- [x] In-app self-update flow (`Update` sidebar entry: GitHub Releases →
      SHA-256 verify → swap → restart, preserves user data)
- [x] Self-healing frontend bundle (`start.sh` downloads the prebuilt
      `dist/` from the matching release when missing — no Node required)
- [x] In-app Donate dialog (BTC address + client-side QR + copy) and
      Donate-hashrate flow (temporary solo.ckpool repoint + auto-revert)
- [x] Guardian VR-temperature frequency governor (AxeOS / Nerd\*)
- [x] MQTT publisher + Home Assistant MQTT-discovery
- [x] LuxOS driver (Antminer S19 / S21, Whatsminer) — read-only monitoring

Next:

- [ ] €/kWh cost calculator + ROI dashboard (Analytics page with Energy
      and Costs sub-tabs)
- [ ] Scheduling work mode based on electricity prices / solar production
- [ ] Anomaly detection (z-score on hashrate / temperature drift)
- [ ] Pool failover (primary / secondary with auto-switch)
- [ ] Webhook out (Discord / Slack / ntfy / Apprise / generic POST)
- [ ] Remote access guidance (Tailscale, reverse tunnel)
- [ ] LuxOS write controls (fan / frequency / power-target / curtailment)
- [ ] Extra drivers: full Antminer line via cgminer

## Contributing

Bug reports, pull requests, and new miner drivers are very welcome — see
[CONTRIBUTING.md](CONTRIBUTING.md) and our
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## License

MinerWatch is released under the **GNU Affero General Public License v3.0**
([full text](LICENSE)).

In short:

- You can run, study, modify, and redistribute it for free.
- If you fork it and distribute the fork, you must release your changes
  under the same license.
- **If you run a modified version as a network service** (e.g. as a hosted
  SaaS), you must make the modified source code available to your users.

This is the same license used by Mastodon, Nextcloud and Plausible
Analytics.

## Disclaimer

MinerWatch is provided **"as is"**, without warranty of any kind. It talks
to your hardware over the LAN; misconfiguration could in theory damage a
device (over-tuning, fan stop, etc.). Use it on equipment you own, on a
network you control, and verify the alert thresholds against your hardware
spec sheet.

This project is not affiliated with Bitaxe, NerdQAxe, Canaan, Braiins,
or other brands.

## Acknowledgements

Special thanks to RedlineGT, Hylinus, No_Mistakes89, trendkraft for sending me logs and helping me test the changes step by step as I implemented them.
